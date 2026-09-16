#!/bin/bash
set -e

HOST_UID="${HOST_UID:?HOST_UID environment variable is required}"
HOST_GID="${HOST_GID:?HOST_GID environment variable is required}"
HOST_USER="${HOST_USER:?HOST_USER environment variable is required}"
ENABLE_DOCKER="${ENABLE_DOCKER:-0}"
HARNESS="${HARNESS:-pi}"
ENABLE_PI_PROVIDER_BRIDGE="${ENABLE_PI_PROVIDER_BRIDGE:-0}"

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
export PATH="$USER_HOME/go/bin:$PATH"
chown "$HOST_UID:$HOST_GID" "$USER_HOME" || true
for stub in "$USER_HOME/.local" "$USER_HOME/.local/share" "$USER_HOME/.cache" "$USER_HOME/.claude"; do
    if [[ -d "$stub" ]] && [[ "$(stat -c '%u' "$stub")" == "0" ]]; then
        chown "$HOST_UID:$HOST_GID" "$stub" || true
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

    # newuidmap / newgidmap need cap_setuid / cap_setgid to write the maps.
    # The file capabilities set by the shadow package can be lost during image
    # layer export, so we reassert them here before starting the daemon.
    setcap cap_setuid+ep /usr/bin/newuidmap
    setcap cap_setgid+ep /usr/bin/newgidmap

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
            PATH="/usr/sbin:/usr/local/bin:/usr/bin:/bin" \
            XDG_RUNTIME_DIR="$runtime_dir" \
            XDG_DATA_HOME="$USER_HOME/.local/share" \
            ${SSL_CERT_FILE:+SSL_CERT_FILE="$SSL_CERT_FILE"} \
        dockerd-rootless.sh \
            --host="unix://$sock" \
            --storage-driver=fuse-overlayfs \
        </dev/null >>"$log" 2>&1 &
    local pid=$!

    echo "Starting rootless Docker daemon (pid=$pid)..." >&2
    sleep 3
    if ! kill -0 "$pid" 2>/dev/null; then
        echo "ERROR: rootless dockerd died during startup. Log:" >&2
        cat "$log" >&2
    elif ! [[ -S "$sock" ]] || ! DOCKER_HOST="unix://$sock" docker version >/dev/null 2>&1; then
        echo "ERROR: rootless dockerd is not responsive. Log:" >&2
        tail -50 "$log" >&2
    else
        echo "Rootless Docker daemon is ready (socket: $sock)." >&2
    fi

    export DOCKER_HOST="unix://$sock"
    export XDG_RUNTIME_DIR="$runtime_dir"
}

[[ "$ENABLE_DOCKER" == "1" ]] && start_rootless_docker

# ---------------------------------------------------------------------------
# Optional: install extra packages requested via --extra-package.
#
# Official-repo packages are installed directly with pacman. Anything pacman
# does not know about is treated as an AUR package: we clone it, install its
# repo dependencies as root, build it as the unprivileged host user (makepkg
# refuses to run as root), then install the built package with pacman -U.
#
# AUR builds are not recursive: only official-repo dependencies are resolved,
# so an AUR package with AUR-only dependencies will fail to build.
# ---------------------------------------------------------------------------
build_aur_package() {
    # Build a single AUR package as the host user and install it as root.
    local pkg="$1"
    local build_dir
    build_dir=$(mktemp -d)
    chown "$HOST_UID:$HOST_GID" "$build_dir"

    if ! runuser -u "$HOST_USER" -- git clone --depth=1 "https://aur.archlinux.org/$pkg.git" "$build_dir/$pkg"; then
        echo "ERROR: failed to clone AUR package '$pkg'" >&2
        rm -rf "$build_dir"
        return 1
    fi

    # Install repo dependencies as root so makepkg can run without sudo.
    local deps
    deps=$(runuser -u "$HOST_USER" -- makepkg --printsrcinfo -D "$build_dir/$pkg" \
        | sed -n 's/^[[:space:]]*\(make\)\?depends = //p' | sed 's/[<>=].*//')
    if [[ -n "$deps" ]]; then
        pacman -S --noconfirm --needed --asdeps $deps
    fi

    if ! runuser -u "$HOST_USER" -- bash -c "cd '$build_dir/$pkg' && makepkg --noconfirm"; then
        echo "ERROR: failed to build AUR package '$pkg'" >&2
        rm -rf "$build_dir"
        return 1
    fi

    local built
    built=$(find "$build_dir/$pkg" -name '*.pkg.tar.zst' | grep -v -- '-debug')
    pacman -U --noconfirm $built
    rm -rf "$build_dir"
}

if [[ -n "${EXTRA_PACKAGES:-}" ]]; then
    echo "Installing extra packages: $EXTRA_PACKAGES" >&2
    pacman -Sy --noconfirm >/dev/null

    repo_packages=()
    aur_packages=()
    for pkg in $EXTRA_PACKAGES; do
        if pacman -Si "$pkg" >/dev/null 2>&1; then
            repo_packages+=("$pkg")
        else
            aur_packages+=("$pkg")
        fi
    done

    [[ ${#repo_packages[@]} -gt 0 ]] && pacman -S --noconfirm --needed "${repo_packages[@]}"

    if [[ ${#aur_packages[@]} -gt 0 ]]; then
        pacman -S --noconfirm --needed base-devel git
        for pkg in "${aur_packages[@]}"; do
            build_aur_package "$pkg"
        done
    fi

    pacman -Scc --noconfirm
fi

# Ensure cache directory exists and is owned by the host user
install -d -o "$HOST_UID" -g "$HOST_GID" "$USER_HOME/.cache"

# Drop root privileges and run the chosen harness as the host user
case "$HARNESS" in
    pi)     exec runuser -u "$HOST_USER" -- env PATH="$PATH" pi "$@" ;;
    claude) exec runuser -u "$HOST_USER" -- env PATH="$PATH"  claude --trust --dangerously-skip-permissions "$@" ;;
    nanobot)
        if [[ "$ENABLE_PI_PROVIDER_BRIDGE" == "1" ]]; then
            case "${1:-}" in
                ""|-h|--help|-v|--version)
                    exec runuser -u "$HOST_USER" -- env env PATH="$PATH" nanobot "$@"
                    ;;
            esac
            for arg in "$@"; do
                case "$arg" in
                    -c|--config|-c=*|--config=*)
                        echo "entrypoint: --config cannot be used with --enable-pi-provider-bridge" >&2
                        exit 2
                        ;;
                esac
            done
            runuser -u "$HOST_USER" -- env PATH="$PATH"  python3 /usr/local/lib/pibox/nanobot_pi_bridge.py
            exec runuser -u "$HOST_USER" -- env PATH="$PATH" nanobot "$@" --config "$USER_HOME/.nanobot/pibox-config.json"
        fi
        exec runuser -u "$HOST_USER" -- env PATH="$PATH" nanobot "$@"
        ;;
    *)      echo "entrypoint: unknown HARNESS: $HARNESS" >&2; exit 2 ;;
esac
