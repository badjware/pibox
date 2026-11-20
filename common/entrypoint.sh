#!/bin/bash
set -e

HOST_UID="${HOST_UID:?HOST_UID environment variable is required}"
HOST_GID="${HOST_GID:?HOST_GID environment variable is required}"
HOST_USER="${HOST_USER:?HOST_USER environment variable is required}"
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
export PATH="$USER_HOME/go/bin:$PATH"
chown "$HOST_UID:$HOST_GID" "$USER_HOME" || true
for stub in "$USER_HOME/.local" "$USER_HOME/.local/share" "$USER_HOME/.cache" "$USER_HOME/.claude" "$USER_HOME/.config"; do
    if [[ -d "$stub" ]] && [[ "$(stat -c '%u' "$stub")" == "0" ]]; then
        chown "$HOST_UID:$HOST_GID" "$stub" || true
    fi
done

# billion-context (pi package) defaults to XDG paths, which are not persisted.
# Keep its config, sessions, and log under ~/.pi, which is bind-mounted.
export BILI_CONFIG_FILE="${BILI_CONFIG_FILE:-$USER_HOME/.pi/agent/billion-context/billion-context.json}"
export BILI_SESSIONS_DIR="${BILI_SESSIONS_DIR:-$USER_HOME/.pi/agent/billion-context/sessions}"
export ACP_LOG_FILE="${ACP_LOG_FILE:-$USER_HOME/.pi/agent/billion-context/bili.log}"

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
    claude) exec runuser -u "$HOST_USER" -- env PATH="$PATH"  claude --dangerously-skip-permissions "$@" ;;
    pi-web) exec runuser -u "$HOST_USER" -- env PATH="$PATH" \
                PI_WEB_HOSTNAME="${PI_WEB_HOSTNAME:-0.0.0.0}" PORT="${PORT:-30141}" pi-web "$@" ;;
    *)      echo "entrypoint: unknown HARNESS: $HARNESS" >&2; exit 2 ;;
esac
