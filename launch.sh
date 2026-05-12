#!/bin/bash -e

REMOTE_IMAGE=""
LOCAL_IMAGE=""
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKDIR="$(pwd -P)"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
HOST_USER="${HOST_USER:-$(id -un)}"

mkdir -p "$HOME/.pi"
mkdir -p "$HOME/.claude"

docker_extra_args=()
tmpworkdir=""
harness_args=()
cleanup() {
    [[ -n "$tmpworkdir" ]] && rm -rf "$tmpworkdir"
}

PARSED=$(getopt -o '' --long 'build,pull,unsafe-enable-docker,ephemeral,tmp,read-only,harness:' -n "$0" -- "$@") || exit 1
eval set -- "$PARSED"

build=0
pull=0
enable_docker=0
ephemeral=0
read_only=""
harness="pi"
while true; do
    case "$1" in
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
        --read-only)
            read_only=1
            shift
            ;;
        --harness)
            harness="$2"
            shift 2
            ;;
        --)
            shift
            break
            ;;
    esac
done

# remaining arguments are passed through to pi inside the container
harness_args=("$@")

# ephemeral mode: use a tmp workdir and don't save the session
if [[ "$ephemeral" -eq 1 ]]; then
    tmpworkdir=$(mktemp -d)
    WORKDIR="$tmpworkdir"
    if [[ "$harness" == "pi" ]]; then
        harness_args=("--no-session" "${harness_args[@]}")
    fi
    trap cleanup EXIT
fi

# resolve image names from harness
case "$harness" in
    pi)     REMOTE_IMAGE="ghcr.io/badjware/pibox:pi"
            LOCAL_IMAGE="pibox:pi" ;;
    claude) REMOTE_IMAGE="ghcr.io/badjware/pibox:claude"
            LOCAL_IMAGE="pibox:claude" ;;
    *)      echo "$0: unknown --harness value: $harness" >&2; exit 2 ;;
esac

# determine which image to use
if [[ "$build" -eq 1 ]]; then
    IMAGE_NAME="$LOCAL_IMAGE"
    npmrc_secret_args=()
    if [[ -f "$HOME/.npmrc" ]]; then
        npmrc_secret_args=("--secret" "id=npmrc,src=$HOME/.npmrc")
    fi
    docker build --pull -t pibox:base -f "$SCRIPT_DIR/Dockerfile.base" "$SCRIPT_DIR"
    docker build -t "$IMAGE_NAME" -f "$SCRIPT_DIR/Dockerfile.$harness" --build-arg BASE_IMAGE=pibox:base \
        "${npmrc_secret_args[@]}" "$SCRIPT_DIR"
else
    IMAGE_NAME="$REMOTE_IMAGE"
    if [[ "$pull" -eq 1 ]]; then
        docker pull "$IMAGE_NAME"
    fi
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
    -e "HARNESS=$harness" \
    \
    -e "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}" \
    -e "ANTHROPIC_DEFAULT_OPUS_MODEL=${ANTHROPIC_DEFAULT_OPUS_MODEL}" \
    -e "ANTHROPIC_DEFAULT_SONNET_MODEL=${ANTHROPIC_DEFAULT_SONNET_MODEL}" \
    -e "ANTHROPIC_DEFAULT_HAIKU_MODEL=${ANTHROPIC_DEFAULT_HAIKU_MODEL}" \
    -e "ANTHROPIC_CUSTOM_HEADERS=${ANTHROPIC_CUSTOM_HEADERS}" \
    -e "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=${CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS}" \
    -e "ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN}" \
    \
    -v "$HOME/.pi:/home/$HOST_USER/.pi${read_only:+:ro}" \
    -v "$SCRIPT_DIR/pi/extensions/pi-claude-interop:/home/$HOST_USER/.pi/agent/extensions/pi-claude-interop:ro" \
    -v "$HOME/.claude:/home/$HOST_USER/.claude${read_only:+:ro}" \
    -v "$HOME/.claude.json:/home/$HOST_USER/.claude.json${read_only:+:ro}" \
    -v "$HOME/.gitconfig:/home/$HOST_USER/.gitconfig:ro" \
    -v "$WORKDIR:$WORKDIR${read_only:+:ro}" \
    -w "$WORKDIR" \
    --ipc=none \
    --pids-limit=512 \
    "${docker_extra_args[@]}" \
    "$IMAGE_NAME" "${harness_args[@]}"
