#!/bin/bash

IMAGE_NAME="pi"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKDIR="$(pwd -P)"

HOST_UID="${HOST_UID:-$(id -u)}"
HOST_GID="${HOST_GID:-$(id -g)}"
HOST_USER="${HOST_USER:-$(id -un)}"

if ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
    docker build --pull -t "$IMAGE_NAME" "$SCRIPT_DIR"
fi

mkdir -p "$HOME/.pi"

docker_extra_args=""
tmpcfg=""
pi_args=()
cleanup() {
    rm -f "$tmpcfg"
}

while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--config-tmpl" && -n "$2" && -f "$2" ]]; then
        tmpcfg=$(mktemp)
        envsubst < "$2" > "$tmpcfg"

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
        shift 2
        continue
    fi

    pi_args+=("$1")
    shift
done


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
