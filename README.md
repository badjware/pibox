# pibox

A containerized, sandboxed environment for running AI coding agents.

Pibox wraps a coding agent inside an Ubuntu-based Docker image so the agent
executes against your working directory without having unrestricted access to
your host system. The container mirrors your host user (UID/GID/name) so files
created inside the container keep consistent ownership on the host.

Two harnesses are supported:

| Harness        | Agent                                                    | Image                           |
| -------------- | -------------------------------------------------------- | ------------------------------- |
| `pi` (default) | [pi](https://github.com/badlogic/pi)                     | `ghcr.io/badjware/pibox:pi`     |
| `claude`       | [Claude Code](https://github.com/anthropics/claude-code) | `ghcr.io/badjware/pibox:claude` |

## Features

- **Sandboxed execution**: the agent runs inside a container with an ephemeral filesystem.
- **Host-user mirroring**: files written from inside the container are owned by your host user.
- **Persistent config**: `~/.pi` and `~/.claude` are bind-mounted so settings and sessions survive between runs.
- **Optional rootless Docker-in-Docker**: opt in with `--unsafe-enable-docker` when the agent needs to run containers itself.
- **Pre-built images**: distributed via GitHub Container Registry.

## Requirements

- Docker

YMMV on WSL.

## Quick start

```sh
git clone https://github.com/badjware/pibox.git
cd pibox
./launch.sh
```

You can invoke `launch.sh` from any directory. The directory you run it from
becomes the working directory bind-mounted into the container.

Set aliases in your shell for convenience:

```sh
alias pibox='/path/to/pibox/launch.sh'
alias claudebox='/path/to/pibox/launch.sh --harness claude'
```

## Usage

```
./launch.sh [--harness pi|claude] [--build] [--pull] [--unsafe-enable-docker] [--ephemeral|--tmp] [--read-only] [-- <agent args>]
```

### Flags

| Flag                     | Description                                                                                                                                                                                                                                                                         |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--harness pi\|claude`   | Choose the agent to run. Defaults to `pi`.                                                                                                                                                                                                                                          |
| `--build`                | Build the image locally from the Dockerfiles instead of using the published image.                                                                                                                                                                                                  |
| `--pull`                 | Update the image prior to launching.                                                                                                                                                                                                                                                |
| `--unsafe-enable-docker` | Start a rootless Docker daemon in DinD mode inside the container so the agent can run containers.                                                                                                                                                                                   |
| `--ephemeral`, `--tmp`   | Start in a temporary working directory instead of the current one. For pi, also disables session persistence (`--no-session`). |
| `--read-only`            | Mount everything as read-only inside the container.                                                                                                                                                                                                                                 |

Any arguments after `--` are passed through to the agent inside the container.

### Examples

Launch pi (default):
```sh
./launch.sh
```

Launch Claude Code:
```sh
./launch.sh --harness claude
```

Pass arguments through to the agent (everything after `--` is forwarded):
```sh
./launch.sh -- -p "summarize the README"
./launch.sh --harness claude -- -p "summarize the README"
```

Force-refresh the image from GHCR:
```sh
./launch.sh --pull
./launch.sh --harness claude --pull
```

Rebuild images locally (useful when iterating on the Dockerfiles):
```sh
./launch.sh --build
./launch.sh --harness claude --build
```

Enable Docker-in-Docker:
```sh
./launch.sh --unsafe-enable-docker
```

## Environment variables

The following environment variables are read from the host and forwarded into
the container:

| Variable                                 | Used by    |
| ---------------------------------------- | ---------- |
| `ANTHROPIC_AUTH_TOKEN`                   | pi, claude |
| `ANTHROPIC_BASE_URL`                     | pi, claude |
| `ANTHROPIC_DEFAULT_OPUS_MODEL`           | pi, claude |
| `ANTHROPIC_DEFAULT_SONNET_MODEL`         | pi, claude |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL`          | pi, claude |
| `ANTHROPIC_CUSTOM_HEADERS`               | claude     |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | claude     |

## What's inside the image

The container ships with a minimal set of tools suited to a coding agent:

- `git`, `vim` (as `$EDITOR`)
- `node`, `python3` (aliased as `python`), `go`
- `fd`, `rg`, `jq`, `yq`, `bc`
- `docker` + `docker compose`

Tools deliberately **not** installed: `sudo`, `ssh`, `scp`, `curl`, `wget`.

## Bind mounts

| Host                      | Container          | Mode |
| ------------------------- | ------------------ | ---- |
| current working directory | same absolute path | rw   |
| `~/.pi`                   | `~/.pi`            | rw   |
| `~/.claude`               | `~/.claude`        | rw   |
| `~/.gitconfig`            | `~/.gitconfig`     | ro   |
