# pibox

A containerized, sandboxed environment for running AI coding agents.

Pibox wraps a coding agent inside an Ubuntu-based Docker image so the agent
executes against your working directory without having unrestricted access to
your host system. The container mirrors your host user (UID/GID/name) so files
created inside the container keep consistent ownership on the host.

Three harnesses are supported:

| Harness        | Agent                                                    | Image                            |
| -------------- | -------------------------------------------------------- | -------------------------------- |
| `pi` (default) | [pi](https://github.com/badlogic/pi)                     | `ghcr.io/badjware/pibox:pi`      |
| `claude`       | [Claude Code](https://github.com/anthropics/claude-code) | `ghcr.io/badjware/pibox:claude`  |
| `nanobot`      | [nanobot](https://github.com/HKUDS/nanobot)              | `ghcr.io/badjware/pibox:nanobot` |

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
./launch.sh [options] [-- <agent args>]
```

### Flags

| Flag                            | Short | Description                                                                                       |
| ------------------------------- | ----- | ------------------------------------------------------------------------------------------------- |
| `--help`                        | `-h`  | Show usage help and exit.                                                                         |
| `--harness pi\|claude\|nanobot` | `-H`  | Choose the agent to run. Defaults to `pi`.                                                        |
| `--build`                       |       | Build the image locally from the Dockerfiles instead of using the published image.                |
| `--pull`                        |       | Update the image prior to launching.                                                              |
| `--unsafe-enable-docker`        |       | Start a rootless Docker daemon in DinD mode inside the container so the agent can run containers. |
| `--unsafe-enable-aws`           |       | Mount `~/.aws` into the container.                                                                |
| `--unsafe-enable-kube`          |       | Mount `~/.kube` into the container.                                                               |
| `--unsafe-host-wayland`         |       | Mount the Wayland socket into the container and forward Wayland environment variables.            |
| `--unsafe-host-tmux`            |       | Mount the tmux socket into the container and forward the tmux environment variable.               |
| `--unsafe-host-net`             |       | Share the host network namespace.                                                                 |
| `--enable-pi-provider-bridge`   |       | Configure nanobot's models from pi. Requires `--harness nanobot`.                                |
| `--ephemeral`, `--tmp`          | `-e`  | Start in a temporary working directory instead of the current one.                                |
| `--read-only`, `--ro`           | `-r`  | Mount all volumes as read-only inside the container.                                              |
| `--volume <spec>`               | `-v`  | Bind-mount an extra volume (repeatable, same syntax as `docker run -v`).                          |
| `--extra-package <name>`        | `-P`  | Install an extra pacman or AUR package at container startup. Repeatable and non-persistent.       |
| `--port <spec>`                 | `-p`  | Publish a container port. Repeatable, using Docker `-p` syntax such as `9119:9119`.               |

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

Launch nanobot with its own configuration:

```sh
./launch.sh --harness nanobot
```

Launch nanobot using models explicitly configured in pi:

```sh
./launch.sh --harness nanobot --enable-pi-provider-bridge
```

The bridge imports models that `pi --offline --list-models` reports as available,
then resolves API keys through `pi auth`. It reads `~/.pi/agent/models.json` for
custom provider settings. It writes `~/.nanobot/pibox-config.json` with owner-only
permissions and leaves `~/.nanobot/config.json` unchanged. OAuth providers and
providers nanobot cannot represent are skipped.

Pass arguments through to the agent (everything after `--` is forwarded):

```sh
./launch.sh -- -p "summarize the README"
```

Install extra packages at startup (repo or AUR):

```sh
./launch.sh -P tree -P openscad-git
```

Packages known to pacman are installed from the official repositories. Anything
else is treated as an AUR package: it is cloned, its dependencies are installed,
and it is built from source at container startup. AUR builds are not recursive,
so an AUR package whose own dependencies are AUR-only will fail to build. This
covers packages whose dependencies all live in the official repositories.

Force-refresh the image from GHCR:

```sh
./launch.sh --pull
```

Forward Wayland for GUI apps:

```sh
./launch.sh --unsafe-host-wayland
```

## Shell completion

A zsh completion is provided at `completions/_pibox`. To enable it, add the
`completions/` directory to your `$fpath` before `compinit` in `~/.zshrc`:

```zsh
fpath=(/path/to/pibox/completions $fpath)
autoload -U compinit && compinit
```

## Environment variables

The following environment variables are read from the host and forwarded into
the container:

| Variable                                 | Used by    |
| ---------------------------------------- | ---------- |
| `ANTHROPIC_AUTH_TOKEN`                   | pi, claude |
| `ANTHROPIC_BASE_URL`                     | claude     |
| `ANTHROPIC_DEFAULT_OPUS_MODEL`           | claude     |
| `ANTHROPIC_DEFAULT_SONNET_MODEL`         | claude     |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL`          | claude     |
| `ANTHROPIC_CUSTOM_HEADERS`               | claude     |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | claude     |

## What's inside the image

The container ships with a minimal set of tools suited to a coding agent:

- `git`, `vim` (as `$EDITOR`)
- `node`, `python3` (aliased as `python`), `go`
- `fd`, `rg`, `jq`, `yq`, `bc`
- `docker` + `docker compose`
- `tmux`

Tools deliberately **not** installed: `sudo`, `ssh`, `scp`, `curl`, `wget`.

## Default bind mounts

These paths are always bind-mounted.

| Host                      | Container          | Mode |
| ------------------------- | ------------------ | ---- |
| current working directory | same absolute path | rw   |
| `~/.pi`                   | `~/.pi`            | rw   |
| `~/.claude`               | `~/.claude`        | rw   |
| `~/.nanobot`              | `~/.nanobot`       | rw   |
| `~/.gitconfig`            | `~/.gitconfig`     | ro   |
