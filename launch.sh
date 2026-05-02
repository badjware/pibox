#!/bin/bash

IMAGE_NAME="pi"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
WORKDIR="$(pwd -P)"

if ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
    docker build -t "$IMAGE_NAME" "$SCRIPT_DIR"
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
        docker_extra_args+=" -v $tmpcfg:/.pi/agent/models.json:ro"
        trap cleanup EXIT
        shift 2
        continue
    fi

    pi_args+=("$1")
    shift
done


docker run --rm -it \
    --user "$(id -u):$(id -g)" \
    -e "ANTHROPIC_AUTH_TOKEN=${ANTHROPIC_AUTH_TOKEN}" \
    -v "$HOME/.pi:/.pi" \
    -v "$WORKDIR:$WORKDIR" \
    -w "$WORKDIR" \
    $docker_extra_args \
    "$IMAGE_NAME" "${pi_args[@]}"
