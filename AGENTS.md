# pibox: Project Notes

## Overview

pibox wraps AI coding agents (pi and Claude Code) inside a sandboxed Docker container.
The working directory is bind-mounted into the container so the agent operates on host files with matching UID/GID ownership.
The workdir mount uses `rslave` propagation (except with `--ephemeral`), so mounts made on the host under the workdir after launch appear in the container. Docker refuses to start if the host mount containing the workdir is private (neither shared nor slave).

## Repository layout

```
launch.sh                           # main entry point: parses flags, builds/pulls image, runs container
Dockerfile.base                     # shared base image (Arch Linux + tools)
Dockerfile.pi                       # extends base; installs @earendil-works/pi-coding-agent
Dockerfile.claude                   # extends base; installs @anthropic-ai/claude-code
common/
  image_AGENTS.md                   # AGENTS.md baked into the image at /AGENTS.md (and /CLAUDE.md for claude)
  entrypoint.sh                     # container entrypoint
```

## launch.sh flags

| Flag | Short | Description |
|---|---|---|
| `--help` | `-h` | show usage help and exit |
| `--harness pi\|claude\|pi-web` | `-H` | agent to run (default: `pi`) |
| `--build` | | build images locally instead of pulling |
| `--pull` | | pull latest image before launch |
| `--unsafe-enable-docker` | (none, intentional) | enable rootless Docker-in-Docker (privileged) |
| `--unsafe-enable-aws` | (none, intentional) | mount `~/.aws` into the container (awscli is pre-installed) |
| `--unsafe-enable-kube` | (none, intentional) | mount `~/.kube` into the container (kubectl is pre-installed) |
| `--unsafe-enable-gcx` | (none, intentional) | mount `~/.config/gcx` into the container (gcx is pre-installed) |
| `--unsafe-host-wayland` | (none, intentional) | mount the Wayland socket into the container and forward Wayland env vars |
| `--unsafe-host-tmux` | (none, intentional) | mount the tmux socket into the container and forward the tmux environment variable |
| `--unsafe-host-net` | (none, intentional) | share the host network namespace (`--network=host`) |
| `--ephemeral`, `--tmp` | | use a temp workdir |
| `--workdir` | `-w` | use the given directory as the workdir instead of the current one (incompatible with `--ephemeral`) |
| `--read-only`, `--ro` | `-r` | mount all volumes read-only |
| `--volume` | `-v` | bind-mount an extra volume (repeatable; same syntax as `docker run -v`, except `PATH` or `PATH:ro\|rw` bind-mounts `PATH` at the same absolute path) |
| `--mount` | `-m` | add a mount (repeatable; same syntax as `docker run --mount`; passed through without deduplication or `--read-only`) |
| `--env` | `-e` | set an environment variable (repeatable; same syntax as `docker run -e`; `VAR` alone copies the host value; can override variables set by pibox) |
| `--extra-package` | `-P` | install an extra pacman or AUR package at container startup (repeatable; non-persistent) |
| `--port` | `-p` | publish a container port (repeatable; Docker `-p` syntax) |
| `--` | | remaining args forwarded to the agent inside the container |

Unsafe options (`--unsafe-*`) intentionally have no short form to reduce the risk of accidental use.

### `--extra-package` and the AUR

`common/entrypoint.sh` installs `--extra-package` requests at container startup. It splits the list by probing `pacman -Si`: packages known to pacman install from the official repositories, everything else is treated as an AUR package. AUR packages are cloned from `aur.archlinux.org`, their repo dependencies are installed as root (parsed from `makepkg --printsrcinfo`), the package is built as the unprivileged host user (makepkg refuses to run as root), then installed with `pacman -U` (the `-debug` package is skipped).

AUR builds are not recursive: an AUR package whose own dependencies are AUR-only will fail because only official-repo dependencies are resolved. This is a deliberate simplification to avoid pulling an AUR helper and `sudo` into the image.

## Shell completions

Zsh completion lives at `completions/_pibox`. When adding, removing, or renaming a flag in `launch.sh`, update `completions/_pibox` to match. The flag list is duplicated by design (no auto-generation); keep the two in sync.

Convention: list only the longest long form of each flag in the completion. Short forms (`-b`, `-p`, etc.) and shorter long-form aliases (e.g. `--tmp` for `--ephemeral`, `--ro` for `--read-only`) still work at the parser level but are omitted from the menu to reduce noise. One entry per flag, no duplicates.

`--help` / `-h` prints a built-in usage summary from `launch.sh`.

## launch.sh guards

`launch.sh` prompts the user for confirmation before proceeding in the following cases:
- running as root
- any `--unsafe-*` option is active (e.g. `--unsafe-enable-docker` enables privileged mode)

When adding new unsafe options, always add a call to the `confirm` helper to prompt the user.

`--sac-moe-patience` is an undocumented escape hatch that skips all confirmation prompts. Do not add it to the public flag table, shell completions, or help text.

## Harnesses and images

| Harness | Remote image | Local image |
|---|---|---|
| `pi` | `ghcr.io/badjware/pibox:pi` | `pibox:pi` |
| `claude` | `ghcr.io/badjware/pibox:claude` | `pibox:claude` |
| `pi-web` | `ghcr.io/badjware/pibox:pi-web` | `pibox:pi-web` |

`~/.claude` (with `~/.claude.json`) is bind-mounted only for the `claude` harness.

Build order when using `--build`: `Dockerfile.base` → `Dockerfile.<harness>`. `pi-web` inherits from `pibox:pi`, so `Dockerfile.pi` is built in between.

### pi-web

`pi-web` runs [Pi Web](https://github.com/agegr/pi-web) (`@agegr/pi-web`). `common/entrypoint.sh` execs `pi-web` on `0.0.0.0:30141` (set through `PI_WEB_HOSTNAME` and `PORT`). `launch.sh` publishes `127.0.0.1:${PIBOX_PI_WEB_PORT:-30141}:30141` unless any `--port` is given, in which case only the user's ports are published. With `--unsafe-host-net`, it sets `PI_WEB_HOSTNAME=127.0.0.1` and `PORT=${PIBOX_PI_WEB_PORT:-30141}` instead, since there is no port mapping. Pi Web keeps its state in `~/.pi/agent`, so it needs no extra mounts. `Dockerfile.pi-web` sets `PI_WEB_NO_OPEN=1`.

## common/image_AGENTS.md

Baked into every image as `/AGENTS.md` (and `/CLAUDE.md` for the claude image).
This is the file that agents inside the container read to understand the runtime environment.
When editing agent-facing instructions that need to persist from one container run to another, edit this file.

## pi extensions

The host's `~/.pi/agent/extensions` directory is bind-mounted into the container, so any pi extensions installed on the host (including `pi-claude-interop`, which lives in its own repo) are available inside pibox automatically. pibox itself no longer ships any bundled extensions.
