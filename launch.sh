#!/bin/bash

IMAGE_NAME="pi"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKDIR="$(pwd -P)"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
HOST_USER="${HOST_USER:-$(id -un)}"

mkdir -p "$HOME/.pi"

docker_extra_args=""
tmpcfg=""
pi_args=()
cleanup() {
    rm -f "$tmpcfg"
}

PARSED=$(getopt -o '' --long 'config-tmpl:,rebuild' -n "$0" -- "$@") || exit 1
eval set -- "$PARSED"

config_tmpl=""
rebuild=0
while true; do
    case "$1" in
        --config-tmpl)
            config_tmpl="$2"
            shift 2
            ;;
        --rebuild)
            rebuild=1
            shift
            ;;
        --)
            shift
            break
            ;;
    esac
done

# Remaining arguments are passed through to pi inside the container
pi_args=("$@")

# check if the image needs to be rebuilt
if [[ "$rebuild" -eq 1 ]] || ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
    docker build --pull -t "$IMAGE_NAME" "$SCRIPT_DIR"
fi

if [[ -n "$config_tmpl" ]]; then
    if [[ ! -f "$config_tmpl" ]]; then
        echo "$0: --config-tmpl: file not found: $config_tmpl" >&2
        exit 1
    fi

    tmpcfg=$(mktemp)
    envsubst < "$config_tmpl" > "$tmpcfg"

    # If a models.json already exists on the host, deep-merge it with the
    # rendered template so that both sets of providers are available inside
    # the container.  Template values take precedence on key conflicts.
    if [[ -f "$HOME/.pi/agent/models.json" ]]; then
        merged=$(mktemp)
        jq -s '.[0] * .[1]' "$HOME/.pi/agent/models.json" "$tmpcfg" > "$merged"
        rm "$tmpcfg"
        tmpcfg="$merged"
    fi

    docker_extra_args+=" -v $tmpcfg:/home/$HOST_USER/.pi/agent/models.json:ro"
    trap cleanup EXIT
fi

exec docker run --rm -it \
    -e "HOST_UID=$HOST_UID" \
    -e "HOST_GID=$HOST_GID" \
    -e "HOST_USER=$HOST_USER" \
    -e "ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN}" \
    -v "$HOME/.pi:/home/$HOST_USER/.pi" \
    -v "$WORKDIR:$WORKDIR" \
    -w "$WORKDIR" \
    $docker_extra_args \
    "$IMAGE_NAME" "${pi_args[@]}"
