#!/bin/bash -e

REMOTE_IMAGE=""
LOCAL_IMAGE=""
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKDIR="$(pwd -P)"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
HOST_USER="${HOST_USER:-$(id -un)}"

skip_confirm=0

usage() {
    cat <<EOF
Usage: $0 [options] [-- agent-args...]

Options:
  -h, --help                    show this help text and exit
  -H, --harness pi|claude       agent to run (default: pi)
  -b, --build                   build images locally instead of pulling
  -p, --pull                    pull latest image before launch
  -e, --ephemeral, --tmp        use a temp workdir
  -r, --read-only, --ro         mount all volumes read-only
  -v, --volume VOLUME           bind-mount an extra volume (repeatable)
  -P, --extra-package PACKAGE   install an extra apt package at startup (repeatable)
      --unsafe-enable-docker    enable rootless Docker-in-Docker (privileged)
      --unsafe-enable-aws       mount ~/.aws into the container
      --unsafe-enable-kube      mount ~/.kube into the container
      --unsafe-host-wayland     mount the Wayland socket into the container
      --unsafe-host-net         share the host network namespace
      --acp                     run the pi-acp adapter instead of pi (pi harness only)

Anything after -- is forwarded to the agent inside the container.
EOF
}

confirm() {
    local msg="$1"
    if [[ "$skip_confirm" -eq 1 ]]; then
        echo "$0: warning: $msg (skipped by --sac-moe-patience)" >&2
        return
    fi
    echo "$0: warning: $msg" >&2
    read -r -p "proceed? [y/N] " reply >&2
    [[ "$reply" =~ ^[yY]$ ]] || exit 1
}

# ensure file exists on host to avoid creating them as root
mkdir -p "$HOME/.pi"
mkdir -p "$HOME/.pi/agent/extensions"
mkdir -p "$HOME/.claude/project"
touch "$HOME/.claude.json"

docker_extra_args=()
tmpworkdir=""
harness_args=()
cleanup() {
    [[ -n "$tmpworkdir" ]] && rm -rf "$tmpworkdir"
}

PARSED=$(getopt -o 'hbperH:v:P:' --long 'help,build,pull,unsafe-enable-docker,unsafe-enable-aws,unsafe-enable-kube,unsafe-host-wayland,unsafe-host-net,ephemeral,tmp,read-only,ro,harness:,volume:,extra-package:,acp,sac-moe-patience' -n "$0" -- "$@") || exit 1
eval set -- "$PARSED"

build=0
pull=0
enable_docker=0
enable_aws=0
enable_kube=0
forward_wayland=0
net_host=0
ephemeral=0
read_only=""
harness="pi"
acp=0
skip_confirm=0
volumes=()
extra_packages=()
while true; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
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
        --unsafe-enable-aws)
            enable_aws=1
            shift
            ;;
        --unsafe-enable-kube)
            enable_kube=1
            shift
            ;;
        --unsafe-host-wayland)
            forward_wayland=1
            shift
            ;;
        --unsafe-host-net)
            net_host=1
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
        --sac-moe-patience)
            skip_confirm=1
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
[[ "$enable_aws" -eq 1 ]] && confirm "--unsafe-enable-aws mounts ~/.aws into the container"
[[ "$enable_kube" -eq 1 ]] && confirm "--unsafe-enable-kube mounts ~/.kube into the container"
[[ "$forward_wayland" -eq 1 ]] && confirm "--unsafe-host-wayland mounts the Wayland socket into the container"
[[ "$net_host" -eq 1 ]] && confirm "--unsafe-host-net shares the host network namespace"

# ensure host dirs exist before bind-mounting so docker doesn't create them as root
[[ "$enable_aws" -eq 1 ]] && mkdir -p "$HOME/.aws"
[[ "$enable_kube" -eq 1 ]] && mkdir -p "$HOME/.kube"

# remaining arguments are passed through to pi inside the container
harness_args=("$@")

# --acp is only supported with the pi harness (uses pi-acp adapter).
if [[ "$acp" -eq 1 && "$harness" != "pi" ]]; then
    echo "$0: --acp is only supported with --harness pi" >&2
    exit 2
fi

# ephemeral mode: use a tmp workdir
if [[ "$ephemeral" -eq 1 ]]; then
    tmpworkdir=$(mktemp -d)
    WORKDIR="$tmpworkdir"
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

# In ACP mode, the in-container harness is pi-acp (not pi).
[[ "$acp" -eq 1 ]] && harness="pi-acp"

# save sessions alongside the original workdir when running in ephemeral mode on pi
if [[ "$ephemeral" -eq 1 && "$harness" =~ ^pi ]]; then
    if [[ ! " ${harness_args[*]} " =~ " --session-dir " ]]; then
        session_dir="$WORKDIR/.pi/sessions"
        mkdir -p "$session_dir"
        harness_args=("${harness_args[@]}" "--session-dir" "$session_dir")
    fi
fi

# determine which image to use
if [[ "$build" -eq 1 ]]; then
    IMAGE_NAME="$LOCAL_IMAGE"
    npmrc_secret_args=()
    if [[ -f "$HOME/.npmrc" ]]; then
        npmrc_secret_args=("--secret" "id=npmrc,src=$HOME/.npmrc")
    fi
    docker build --pull -t pibox:base -f "$SCRIPT_DIR/Dockerfile.base" \
        "${npmrc_secret_args[@]}" "$SCRIPT_DIR"
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
        "--security-opt=seccomp=unconfined"
        "--cap-drop=ALL"
        "--cap-add=CHOWN"
        "--cap-add=DAC_OVERRIDE"
        "--cap-add=FOWNER"
        "--cap-add=SETUID"
        "--cap-add=SETGID"
        "--cap-add=KILL"
    )
fi

if [[ "$net_host" -eq 1 ]]; then
    docker_extra_args+=("--network=host")
fi

if [[ "$forward_wayland" -eq 1 ]]; then
    if [[ -z "${WAYLAND_DISPLAY:-}" ]]; then
        echo "$0: --unsafe-host-wayland requires WAYLAND_DISPLAY to be set" >&2
        exit 2
    fi

    wayland_socket="${XDG_RUNTIME_DIR:-/run/user/$HOST_UID}/$WAYLAND_DISPLAY"
    if [[ ! -S "$wayland_socket" ]]; then
        echo "$0: Wayland socket not found: $wayland_socket" >&2
        exit 2
    fi

    docker_extra_args+=(
        "-v" "$wayland_socket:$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY:rw"
        "-e" "XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"
        "-e" "WAYLAND_DISPLAY=$WAYLAND_DISPLAY"
        "-e" "XDG_SESSION_TYPE=wayland"
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
_vol_register "$HOME/.pi:/home/$HOST_USER/.pi:rw"
_vol_register "$HOME/.pi/agent/extensions:/home/$HOST_USER/.pi/agent/extensions:ro" # extensions are read-only at runtime
_vol_register "$HOME/.claude:/home/$HOST_USER/.claude:ro"
_vol_register "$HOME/.claude/project:/home/$HOST_USER/.claude/project:rw" # claude projects folder is always rw
_vol_register "$HOME/.claude.json:/home/$HOST_USER/.claude.json:rw" # claude really hates to have its config file read-only
_vol_register "$HOME/.gitconfig:/home/$HOST_USER/.gitconfig:ro"
_vol_register "pibox-cache:/home/$HOST_USER/.cache:rw"
_vol_register "/etc/fonts:/etc/fonts:ro"
_vol_register "/usr/share/fonts:/usr/share/fonts:ro"
_vol_register "/var/cache/fontconfig:/var/cache/fontconfig:ro"

# Host CA bundle: prefer $SSL_CERT_FILE if set on the host, else probe a
# short list of well-known per-distro paths.
host_ca="${SSL_CERT_FILE:-}"
if [[ -z "$host_ca" ]]; then
    for p in \
        /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem \
        /etc/ssl/certs/ca-certificates.crt \
        /etc/ssl/ca-bundle.pem \
        /etc/pki/tls/certs/ca-bundle.crt \
        /etc/ssl/cert.pem
    do
        [[ -f "$p" ]] && host_ca="$p" && break
    done
fi
if [[ -n "$host_ca" && -f "$host_ca" ]]; then
    _vol_register "$host_ca:/etc/ssl/host-ca-bundle.pem:ro"
fi
[[ "$enable_aws" -eq 1 ]] && _vol_register "$HOME/.aws:/home/$HOST_USER/.aws:ro"
[[ "$enable_kube" -eq 1 ]] && _vol_register "$HOME/.kube:/home/$HOST_USER/.kube:ro"

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

host_tz="${TZ:-}"
if [[ -z "$host_tz" && -L /etc/localtime ]]; then
    host_tz="$(readlink /etc/localtime | sed 's|.*/zoneinfo/||')"
fi

exec docker run --rm \
    -e "TZ=${host_tz}" \
    -e "COLORTERM=${COLORTERM}" \
    -e "TERM=${TERM}" \
    -e "HOST_UID=$HOST_UID" \
    -e "HOST_GID=$HOST_GID" \
    -e "HOST_USER=$HOST_USER" \
    -e "HARNESS=$harness" \
    -e "EXTRA_PACKAGES=${extra_packages[*]}" \
    ${host_ca:+-e "SSL_CERT_FILE=/etc/ssl/host-ca-bundle.pem"} \
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
    --pids-limit=1024 \
    "${docker_extra_args[@]}" \
    "$IMAGE_NAME" "${harness_args[@]}"
