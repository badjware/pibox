#!/bin/bash -e

REMOTE_IMAGE="ghcr.io/badjware/pibox:latest"
LOCAL_IMAGE="pi"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKDIR="$(pwd -P)"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
HOST_USER="${HOST_USER:-$(id -un)}"

mkdir -p "$HOME/.pi"
mkdir -p "$HOME/.claude"

docker_extra_args=()
tmpcfg=""
tmpworkdir=""
pi_args=()
cleanup() {
    rm -f "$tmpcfg"
    [[ -n "$tmpworkdir" ]] && rm -rf "$tmpworkdir"
}

PARSED=$(getopt -o '' --long 'config-tmpl:,build,pull,unsafe-enable-docker,ephemeral,tmp' -n "$0" -- "$@") || exit 1
eval set -- "$PARSED"

config_tmpl=""
build=0
pull=0
enable_docker=0
ephemeral=0
while true; do
    case "$1" in
        --config-tmpl)
            config_tmpl="$2"
            shift 2
            ;;
        --build)
            build=1
            shift
            ;;
        --pull)
            pull=1
            shift
            ;;
        --unsafe-enable-docker)
            enable_docker=1
            shift
            ;;
        --ephemeral|--tmp)
            ephemeral=1
            shift
            ;;
        --)
            shift
            break
            ;;
    esac
done

# remaining arguments are passed through to pi inside the container
pi_args=("$@")

# ephemeral mode: use a tmp workdir and don't save the pi session
if [[ "$ephemeral" -eq 1 ]]; then
    tmpworkdir=$(mktemp -d)
    WORKDIR="$tmpworkdir"
    pi_args=("--no-session" "${pi_args[@]}")
    trap cleanup EXIT
fi

# determine which image to use
if [[ "$build" -eq 1 ]]; then
    IMAGE_NAME="$LOCAL_IMAGE"
    docker build --pull -t "$IMAGE_NAME" "$SCRIPT_DIR"
else
    IMAGE_NAME="$REMOTE_IMAGE"
    if [[ "$pull" -eq 1 ]]; then
        docker pull "$IMAGE_NAME"
    fi
fi

if [[ -n "$config_tmpl" ]]; then
    if [[ ! -f "$config_tmpl" ]]; then
        echo "$0: --config-tmpl: file not found: $config_tmpl" >&2
        exit 1
    fi

    tmpcfg=$(mktemp)
    envsubst < "$config_tmpl" > "$tmpcfg"

    # If a models.json already exists on the host, merge it with the
    # rendered template so that both sets of providers are available inside
    # the container.  Template values take precedence on key conflicts.
    if [[ -f "$HOME/.pi/agent/models.json" ]]; then
        merged=$(mktemp)
        jq -s '.[0] * .[1]' "$HOME/.pi/agent/models.json" "$tmpcfg" > "$merged"
        rm "$tmpcfg"
        tmpcfg="$merged"
    fi

    docker_extra_args+=("-v" "$tmpcfg:/home/$HOST_USER/.pi/agent/models.json:ro")
    trap cleanup EXIT
fi

# rootless Docker-in-Docker: run the outer container as privileged so that
# the inner rootless dockerd can create user namespaces and use fuse-overlayfs.
if [[ "$enable_docker" -eq 1 ]]; then
    docker_extra_args+=(
        "--privileged"
        "-e" "ENABLE_DOCKER=1"
    )
else
    # Drop all caps and re-add only what the entrypoint needs.
    # Also block setuid/setgid binaries from gaining new privileges.
    docker_extra_args+=(
        "--security-opt=no-new-privileges:true"
        "--cap-drop=ALL"
        "--cap-add=CHOWN"
        "--cap-add=DAC_OVERRIDE"
        "--cap-add=FOWNER"
        "--cap-add=SETUID"
        "--cap-add=SETGID"
        "--cap-add=KILL"
    )
fi

# check if we are in a tty
[[ -t 0 && -t 1 ]] && docker_extra_args+=("-it")

exec docker run --rm \
    -e "HOST_UID=$HOST_UID" \
    -e "HOST_GID=$HOST_GID" \
    -e "HOST_USER=$HOST_USER" \
    -e "ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN}" \
    -v "$HOME/.pi:/home/$HOST_USER/.pi" \
    -v "$HOME/.claude:/home/$HOST_USER/.claude:ro" \
    -v "$HOME/.gitconfig:/home/$HOST_USER/.gitconfig:ro" \
    -v "$WORKDIR:$WORKDIR" \
    -w "$WORKDIR" \
    --ipc=none \
    --pids-limit=512 \
    "${docker_extra_args[@]}" \
    "$IMAGE_NAME" "${pi_args[@]}"
