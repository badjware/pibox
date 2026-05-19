#!/bin/bash -e

REMOTE_IMAGE=""
LOCAL_IMAGE=""
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKDIR="$(pwd -P)"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
HOST_USER="${HOST_USER:-$(id -un)}"

confirm() {
    local msg="$1"
    echo "$0: warning: $msg" >&2
    read -r -p "proceed? [y/N] " reply >&2
    [[ "$reply" =~ ^[yY]$ ]] || exit 1
}

mkdir -p "$HOME/.pi"
mkdir -p "$HOME/.claude"

docker_extra_args=()
tmpworkdir=""
harness_args=()
cleanup() {
    [[ -n "$tmpworkdir" ]] && rm -rf "$tmpworkdir"
}

PARSED=$(getopt -o 'bperH:v:P:' --long 'build,pull,unsafe-enable-docker,ephemeral,tmp,read-only,ro,harness:,volume:,extra-package:,acp' -n "$0" -- "$@") || exit 1
eval set -- "$PARSED"

build=0
pull=0
enable_docker=0
ephemeral=0
read_only=""
harness="pi"
acp=0
volumes=()
extra_packages=()
while true; do
    case "$1" in
        -b|--build)
            build=1
            shift
            ;;
        -p|--pull)
            pull=1
            shift
            ;;
        --unsafe-enable-docker)
            enable_docker=1
            shift
            ;;
        -e|--ephemeral|--tmp)
            ephemeral=1
            shift
            ;;
        -r|--read-only|--ro)
            read_only=1
            shift
            ;;
        -H|--harness)
            harness="$2"
            shift 2
            ;;
        -v|--volume)
            volumes+=("$2")
            shift 2
            ;;
        -P|--extra-package)
            extra_packages+=("$2")
            shift 2
            ;;
        --acp)
            acp=1
            shift
            ;;
        --)
            shift
            break
            ;;
    esac
done


# warn about root
if [[ "$HOST_UID" -eq 0 ]]; then
    confirm "running as root"
fi

# warn about active unsafe options
[[ "$enable_docker" -eq 1 && "$acp" -ne 1 ]] && confirm "--unsafe-enable-docker enables privileged mode"

# remaining arguments are passed through to pi inside the container
harness_args=("$@")

# --acp is only supported with the pi harness (uses pi-acp adapter).
if [[ "$acp" -eq 1 && "$harness" != "pi" ]]; then
    echo "$0: --acp is only supported with --harness pi" >&2
    exit 2
fi

# ephemeral mode: use a tmp workdir and don't save the session
if [[ "$ephemeral" -eq 1 ]]; then
    tmpworkdir=$(mktemp -d)
    WORKDIR="$tmpworkdir"
    # --no-session is a pi flag; pi-acp manages sessions on its own.
    if [[ "$harness" == "pi" && "$acp" -eq 0 ]]; then
        harness_args=("--no-session" "${harness_args[@]}")
    fi
    trap cleanup EXIT
fi

# resolve image names from harness
# (--acp reuses the pi image; pi-acp is installed alongside pi.)
case "$harness" in
    pi)     REMOTE_IMAGE="ghcr.io/badjware/pibox:pi"
            LOCAL_IMAGE="pibox:pi" ;;
    claude) REMOTE_IMAGE="ghcr.io/badjware/pibox:claude"
            LOCAL_IMAGE="pibox:claude" ;;
    *)      echo "$0: unknown --harness value: $harness" >&2; exit 2 ;;
esac

# In ACP mode, the in-container harness is pi-acp (not pi).
[[ "$acp" -eq 1 ]] && harness="pi-acp"

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

# Resolve the final volume list.
# Priority: defaults < user-provided < workdir.
# Volumes with the same container destination are deduplicated; higher priority wins.
# Insertion order of destinations is important.
declare -A _vol_map   # container destination -> full volume spec
_vol_keys=()          # destinations in insertion order

# Register a volume spec. First registration wins the slot; later calls for the
# same destination overwrite the spec (allowing higher-priority tiers to win).
_vol_register() {
    local spec="$1"
    # Extract container destination: strip leading "src:" then trailing ":opts"
    local dest="${spec#*:}"
    dest="${dest%%:*}"
    [[ -z "${_vol_map[$dest]+x}" ]] && _vol_keys+=("$dest")
    _vol_map[$dest]="$spec"
}

# defaults has lowest priority, so we register them first
_vol_register "$HOME/.pi:/home/$HOST_USER/.pi${read_only:+:ro}"
_vol_register "$HOME/.pi/sessions:/home/$HOST_USER/.pi/agent/sessions:rw" # pi sessions folder is always rw
_vol_register "$SCRIPT_DIR/pi/extensions/pi-claude-interop:/home/$HOST_USER/.pi/agent/extensions/pi-claude-interop:ro"
_vol_register "$HOME/.claude:/home/$HOST_USER/.claude${read_only:+:ro}"
_vol_register "$HOME/.claude/project:/home/$HOST_USER/.claude/project:rw" # claude projects folder is always rw
_vol_register "$HOME/.claude.json:/home/$HOST_USER/.claude.json:rw" # claude really hates to have its config file read-only
_vol_register "$HOME/.gitconfig:/home/$HOST_USER/.gitconfig:ro"

# user-provided
for vol in "${volumes[@]}"; do
    _vol_register "$vol"
done

# workdir has highest priority, so we register it last
_vol_register "$WORKDIR:$WORKDIR${read_only:+:ro}"

for dest in "${_vol_keys[@]}"; do
    docker_extra_args+=("-v" "${_vol_map[$dest]}")
done

# In ACP mode, the editor (e.g. Zed) speaks JSON-RPC 2.0 over stdio to the
# spawned process. We must attach stdin (-i) but never allocate a TTY (-t),
# which would wrap stdout in a PTY and corrupt JSON-RPC framing.
if [[ "$acp" -eq 1 ]]; then
    docker_extra_args+=("-i")
elif [[ -t 0 && -t 1 ]]; then
    docker_extra_args+=("-it")
fi

exec docker run --rm \
    -e "HOST_UID=$HOST_UID" \
    -e "HOST_GID=$HOST_GID" \
    -e "HOST_USER=$HOST_USER" \
    -e "HARNESS=$harness" \
    -e "EXTRA_PACKAGES=${extra_packages[*]}" \
    \
    -e "ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}" \
    -e "ANTHROPIC_DEFAULT_OPUS_MODEL=${ANTHROPIC_DEFAULT_OPUS_MODEL}" \
    -e "ANTHROPIC_DEFAULT_SONNET_MODEL=${ANTHROPIC_DEFAULT_SONNET_MODEL}" \
    -e "ANTHROPIC_DEFAULT_HAIKU_MODEL=${ANTHROPIC_DEFAULT_HAIKU_MODEL}" \
    -e "ANTHROPIC_CUSTOM_HEADERS=${ANTHROPIC_CUSTOM_HEADERS}" \
    -e "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=${CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS}" \
    -e "ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN}" \
    -w "$WORKDIR" \
    --ipc=none \
    --pids-limit=512 \
    "${docker_extra_args[@]}" \
    "$IMAGE_NAME" "${harness_args[@]}"
