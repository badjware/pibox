# pibox

A containerized, sandboxed environment for running AI coding agents.

Pibox wraps a coding agent inside an Arch Linux-based Docker image so the agent
executes against your working directory without having unrestricted access to
your host system. The container mirrors your host user (UID/GID/name) so files
created inside the container keep consistent ownership on the host.

Three harnesses are supported:

| Harness        | Agent                                                      | Image                            |
| -------------- | ---------------------------------------------------------- | -------------------------------- |
| `pi` (default) | [pi](https://github.com/earendil-works/pi)                 | `ghcr.io/badjware/pibox:pi`      |
| `claude`       | [Claude Code](https://github.com/anthropics/claude-code)   | `ghcr.io/badjware/pibox:claude`  |
| `pi-web`       | [Pi Web](https://github.com/agegr/pi-web), a web UI for pi | `ghcr.io/badjware/pibox:pi-web`  |

## Features

- **Sandboxed execution**: the agent runs inside a container that is deleted when the session ends.
- **Host-user mirroring**: files written from inside the container are owned by your host user.
- **Persistent config**: `~/.pi` is bind-mounted, and `~/.claude` is bind-mounted for the claude harness, so settings and sessions survive between runs.
- **Optional rootless Docker**: opt in with `--unsafe-enable-docker` when the agent needs to run containers itself. The daemon runs in a privileged sidecar container, so the agent container is never privileged.
- **Pre-built images**: distributed via GitHub Container Registry.

## Requirements

- Docker
- Python 3

YMMV on WSL.

## Quick start

```sh
git clone https://github.com/badjware/pibox.git
cd pibox
./launch.py
```

You can invoke `launch.py` from any directory. The directory you run it from
becomes the working directory bind-mounted into the container.

Set aliases in your shell for convenience:

```sh
alias pibox='/path/to/pibox/launch.py'
alias claudebox='/path/to/pibox/launch.py --harness claude'
```

## Usage

```
./launch.py [options] [-- <agent args>]
```

### Flags

| Flag                                    | Short | Description                                                                                                                   |
| --------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------- |
| `--help`                                | `-h`  | Show usage help and exit.                                                                                                     |
| `--harness pi\|claude\|pi-web`          | `-H`  | Choose the agent to run. Defaults to `pi`.                                                                                    |
| `--build`                               |       | Build the image locally from the Dockerfiles instead of using the published image.                                            |
| `--pull`                                |       | Update the image prior to launching.                                                                                          |
| `--unsafe-enable-docker`                |       | Start a rootless Docker daemon in a privileged sidecar container so the agent can run containers.                             |
| `--unsafe-enable-aws`                   |       | Mount `~/.aws` into the container.                                                                                            |
| `--unsafe-enable-kube`                  |       | Mount `~/.kube` into the container.                                                                                           |
| `--unsafe-enable-gcx`                   |       | Mount `~/.config/gcx` into the container.                                                                                     |
| `--unsafe-host-wayland`                 |       | Mount the Wayland socket into the container and forward Wayland environment variables.                                        |
| `--unsafe-host-net`                     |       | Share the host network namespace.                                                                                             |
| `--read-only`                           | `-r`  | Mount the working directory read-only. It is always read-only from `$HOME`, unless a `--volume` for `$HOME` is given.         |
| `--volume <spec>`                       | `-v`  | Bind-mount an extra volume (repeatable, same syntax as `docker run -v`).                                                      |
| `--env <var>[=<value>]`                 | `-e`  | Set an environment variable in the container (repeatable, same syntax as `docker run -e`).                                    |
| `--extra-package <name>`                | `-P`  | Install an extra pacman or AUR package at container startup. Repeatable and non-persistent.                                   |
| `--port <spec>`                         | `-p`  | Publish a container port. Repeatable, using Docker `-p` syntax such as `9119:9119`. Replaces the default pi-web port mapping. |

Any arguments after `--` are passed through to the agent inside the container.

### Examples

Launch pi (default):

```sh
./launch.py
```

Launch Claude Code:

```sh
./launch.py --harness claude
```

Launch Pi Web and open http://127.0.0.1:30141:

```sh
./launch.py --harness pi-web
```

By default, pi-web is published on `127.0.0.1:30141` only. Any `--port` replaces
that mapping. pi-web listens on port 30141 inside the container, so target it
explicitly, for example `--port 0.0.0.0:9000:30141`. Set `PIBOX_PI_WEB_PORT` to
change the default host port. With `--unsafe-host-net`, pi-web binds
`127.0.0.1:${PIBOX_PI_WEB_PORT:-30141}` directly on the host. Pi Web has no
authentication by default; only expose it on a trusted network.

Pass arguments through to the agent (everything after `--` is forwarded):

```sh
./launch.py -- -p "summarize the README"
```

Install extra packages at startup (repo or AUR):

```sh
./launch.py -P tree -P openscad-git
```

Packages known to pacman are installed from the official repositories. Anything
else is treated as an AUR package: it is cloned, its dependencies are installed,
and it is built from source at container startup. AUR builds are not recursive,
so an AUR package whose own dependencies are AUR-only will fail to build. This
covers packages whose dependencies all live in the official repositories.

Force-refresh the image from GHCR:

```sh
./launch.py --pull
```

Forward Wayland for GUI apps:

```sh
./launch.py --unsafe-host-wayland
```

With `--unsafe-enable-docker`, sessions in the same working directory share one
Docker daemon. Its images and build cache persist between sessions in a volume
per working directory, named `pibox-docker-<path>` (`-ro` with `--read-only`).
pibox never removes these volumes. List and remove them with:

```sh
docker volume ls --filter name=pibox-docker-
docker volume rm pibox-docker-home-me-develop-project
```

## Shell completion

A zsh completion is provided at `completions/_pibox`. To enable it, add the
`completions/` directory to your `$fpath` before `compinit` in `~/.zshrc`:

```zsh
fpath=(/path/to/pibox/completions $fpath)
autoload -U compinit && compinit
```

## What's inside the image

The container ships with a set of tools suited to a coding agent:

- `git`, `vim` (as `$EDITOR`), `tmux`
- `node`, `python` (Python 3), `go`, `gcc`, `make`
- `fd`, `rg`, `jq`, `yq`, `bc`, `zip`, `unzip`, `diffutils`, `curl`
- `docker` + `docker compose`
- `kubectl`, `aws` (aws-cli), `gcx` (Grafana CLI)
- `camoufox` (Firefox-based browser and Node module), `wl-clipboard`, `trafilatura`

Tools deliberately **not** installed: `sudo`, `ssh`, `scp`, `wget`.

## Default bind mounts

These paths are bind-mounted when they exist on the host. pibox does not create
them, so Docker does not create them as root.

| Host                              | Container                     | Mode |
| --------------------------------- | ----------------------------- | ---- |
| current working directory         | same absolute path            | rw (ro from `$HOME`) |
| `~/.pi`                           | `~/.pi`                       | rw   |
| `~/.pi/agent/extensions`          | `~/.pi/agent/extensions`      | ro   |
| `~/.claude` (claude only)         | `~/.claude`                   | rw   |
| `~/.claude.json` (claude only)    | `~/.claude.json`              | rw   |
| `~/.gitconfig`                    | `~/.gitconfig`                | ro   |
| `pibox-cache` volume              | `~/.cache`                    | rw   |
| host CA bundle                    | `/etc/ssl/host-ca-bundle.pem` | ro   |
