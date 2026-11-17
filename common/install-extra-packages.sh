#!/bin/bash
# Install --extra-package requests while building the per-user image.
#
# Usage: install-extra-packages.sh BUILD_USER PACKAGE...
#
# Runs as root. Official-repo packages are installed directly with pacman.
# Anything pacman does not know about is treated as an AUR package: we clone
# it, install its repo dependencies, build it as BUILD_USER (makepkg refuses
# to run as root), then install the built package with pacman -U.
#
# AUR builds are not recursive: only official-repo dependencies are resolved,
# so an AUR package with AUR-only dependencies will fail to build.
set -e

build_user="$1"
shift

build_aur_package() {
    # Build a single AUR package as build_user and install it as root.
    local pkg="$1"
    local build_dir
    build_dir=$(mktemp -d)
    chown "$build_user:" "$build_dir"

    runuser -u "$build_user" -- git clone --depth=1 "https://aur.archlinux.org/$pkg.git" "$build_dir/$pkg"

    # Install repo dependencies as root so makepkg can run without sudo.
    local deps
    deps=$(runuser -u "$build_user" -- makepkg --printsrcinfo -D "$build_dir/$pkg" \
        | sed -n 's/^[[:space:]]*\(make\)\?depends = //p' | sed 's/[<>=].*//')
    if [[ -n "$deps" ]]; then
        pacman -S --noconfirm --needed --asdeps $deps
    fi

    runuser -u "$build_user" -- bash -c "cd '$build_dir/$pkg' && makepkg --noconfirm"

    local built
    built=$(find "$build_dir/$pkg" -name '*.pkg.tar.zst' | grep -v -- '-debug')
    pacman -U --noconfirm $built
    rm -rf "$build_dir"
}

# Arch does not support partial upgrades: packages from a fresh sync may need
# newer libraries than the image has.
pacman -Syu --noconfirm >/dev/null

repo_packages=()
aur_packages=()
for pkg in "$@"; do
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
