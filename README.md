# pibox

A containerized, sandboxed environment for running [pi](https://github.com/badlogic/pi).

Pibox wraps `pi` inside an Ubuntu-based Docker image so the agent executes against your working directory without having unrestricted access to your host system. The container mirrors your host user (UID/GID/name) so files created inside the container keep consistent ownership on the host.

## Features

- **Sandboxed execution**: `pi` runs inside a container with an ephemeral filesystem.
- **Host-user mirroring**: files written from inside the container are owned by your host user.
- **Persistent config**: `~/.pi` is bind-mounted so settings survive between runs.
- **Optional rootless Docker-in-Docker**: opt in with `--unsafe-enable-docker` when the agent needs to run containers itself.
- **Config templating**: render a `models.json` from a template with environment variables injected at launch time.
- **Pre-built image**: distributed via GitHub Container Registry.

## Requirements

- Docker
- `envsubst` (from `gettext`), `jq`

YMMV on WSL.

## Quick start

```sh
git clone https://github.com/badjware/pibox.git
cd pibox
./launch.sh
```

You can invoke `launch.sh` from any directory. The directory you run it from becomes the working directory bind-mounted into the container.

You may set an alias in your shell of choice for convenience:

```sh
alias pibox='/path/to/pibox/launch.sh'
```

## Usage

```
./launch.sh [--build] [--pull] [--unsafe-enable-docker] [--config-tmpl PATH] [--ephemeral|--tmp] [-- <pi args>]
```

### Flags

| Flag | Description |
|---|---|
| `--build` | Build the image locally from the `Dockerfile` instead of using the published image. |
| `--pull` | Update the image prior to launching. |
| `--unsafe-enable-docker` | Start a rootless Docker daemon in DinD mode inside the container so the agent can run containers. |
| `--config-tmpl PATH` | Render a `models.json` template through `envsubst` and mount it into `~/.pi/agent/models.json` inside the container. Merged with any existing host-side `models.json` via `jq` (template values take precedence). |
| `--ephemeral`, `--tmp` | Start in a temporary working directory instead of the current one, and disable pi session persistence (`--no-session`). The tmp directory is removed when the container exits. |

Any arguments after `--` are passed through to `pi` inside the container.

### Examples

Launch with the default published image:
```sh
./launch.sh
```

Force-refresh the image from GHCR:
```sh
./launch.sh --pull
```

Rebuild the image locally (useful when iterating on the `Dockerfile`):
```sh
./launch.sh --build
```

Enable Docker-in-Docker:
```sh
./launch.sh --unsafe-enable-docker
```

Pass arguments through to `pi` (everything after `--` is forwarded):
```sh
./launch.sh -- -p "summarize the README"
```

## What's inside the image

The container ships with a minimal set of tools suited to a coding agent:

- `git`, `vim` (as `$EDITOR`)
- `node`, `python3` (aliased as `python`), `go`
- `fd`, `rg`, `jq`, `yq`, `bc`
- `docker` + `docker compose`

Tools deliberately **not** installed: `sudo`, `ssh`, `scp`, `curl`, `wget`.

## Bind mounts

| Host | Container | Mode |
|---|---|---|
| current working directory | same absolute path | rw |
| `~/.pi` | `~/.pi` | rw |
| `~/.claude` | `~/.claude` | ro |
| `~/.gitconfig` | `~/.gitconfig` | ro |
