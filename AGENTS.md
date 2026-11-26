# pibox: Project Notes

## Overview

pibox wraps AI coding agents (pi and Claude Code) inside a sandboxed Docker container.
The working directory is bind-mounted into the container so the agent operates on host files with matching UID/GID ownership.
The workdir mount uses `rslave` propagation, so mounts made on the host under the workdir after launch appear in the container. Docker refuses to start if the host mount containing the workdir is private (neither shared nor slave).

## Repository layout

```
launch.py                           # main entry point: parses flags, builds/pulls image, runs container
Dockerfile.base                     # shared base image (Arch Linux + tools)
Dockerfile.pi                       # extends base; installs @earendil-works/pi-coding-agent
Dockerfile.claude                   # extends base; installs @anthropic-ai/claude-code
Dockerfile.pi-web                   # extends pi; installs @agegr/pi-web
common/
  image_AGENTS.md                   # AGENTS.md baked into the image at /AGENTS.md (and /CLAUDE.md for claude)
  entrypoint.sh                     # container entrypoint
contrib/systemd/                    # pi-web user units
completions/_pibox                  # zsh completion
```

`launch.py` uses only the Python standard library and reads no file from the repo at runtime, so it works when copied alone (for example to `~/.local/bin/pibox`). Only `--build` uses the Dockerfiles.

## Mounts

`launch.py` keeps a volume table keyed by container destination (defaults < user `--volume` < workdir; a later entry for the same destination wins) and passes it to `docker run -v`.

- `launch.py` creates no file or directory on the host. A default mount whose host path does not exist is skipped, so Docker does not create it as root. This includes `~/.pi`, `~/.claude`, `~/.claude.json`, `~/.gitconfig`, and the `--unsafe-enable-aws/kube/gcx` directories.
- When launched from `$HOME`, the workdir is read-only unless a `--volume` for `$HOME` is given.
- `--read-only` applies to the workdir only.
- The container is named after the workdir, like the user's tmux sessions: `home` for `$HOME`, else the directory name lowercased with characters outside `[a-z0-9-]` replaced by `-`, then `-2`, `-3`, ... when `docker ps -a` already has the name.

## launch.py flags

| Flag | Short | Description |
|---|---|---|
| `--help` | `-h` | show usage help and exit |
| `--harness pi\|claude\|pi-web` | `-H` | agent to run (default: `pi`) |
| `--build` | | build images locally instead of pulling |
| `--pull` | | pull latest image before launch |
| `--unsafe-enable-docker` | (none, intentional) | start a rootless dockerd in a privileged sidecar container and share its socket |
| `--unsafe-enable-aws` | (none, intentional) | mount `~/.aws` into the container (awscli is pre-installed) |
| `--unsafe-enable-kube` | (none, intentional) | mount `~/.kube` into the container (kubectl is pre-installed) |
| `--unsafe-enable-gcx` | (none, intentional) | mount `~/.config/gcx` into the container (gcx is pre-installed) |
| `--unsafe-host-wayland` | (none, intentional) | mount the Wayland socket into the container and forward Wayland env vars |
| `--unsafe-host-net` | (none, intentional) | share the host network namespace (`--network=host`) |
| `--read-only` | `-r` | mount the workdir read-only |
| `--volume` | `-v` | bind-mount an extra volume (repeatable; same syntax as `docker run -v`, except `PATH` or `PATH:ro\|rw` bind-mounts `PATH` at the same absolute path) |
| `--env` | `-e` | set an environment variable (repeatable; same syntax as `docker run -e`; `VAR` alone copies the host value; overrides variables set by pibox) |
| `--extra-package` | `-P` | install an extra pacman or AUR package at container startup (repeatable; non-persistent) |
| `--port` | `-p` | publish a container port (repeatable; Docker `-p` syntax) |
| `--` | | remaining args forwarded to the agent inside the container |

Unsafe options (`--unsafe-*`) intentionally have no short form to reduce the risk of accidental use.

### `--unsafe-enable-docker`

The agent container never runs privileged. Sessions with the same workdir mount spec (path and mode) share one privileged sidecar, `<workdir name>-docker` (then `-docker-2`, ...), from the official `docker:dind-rootless` image (`DIND_IMAGE`; `--pull` also pulls it). The sidecar starts as root and runs `DIND_SCRIPT`, embedded in `launch.py`: the image only has `/etc/passwd` and `/etc/subuid` entries for its own user (UID 1000), so the script adds the host user and its subordinate ranges, then runs the image entrypoint `dockerd-entrypoint.sh` as the host user. The daemon must run as the host UID so that files that containers write in the workdir belong to the host user. The script passes an explicit `dockerd --host=unix://...`, because without arguments the image entrypoint also listens on TCP. The daemon data (`dockerd --data-root=/pibox-docker-data`) is the named volume `pibox-docker-<workdir path>[-ro]` (the path lowercased, characters outside `[a-z0-9-]` replaced by `-`), so images and the build cache persist between sessions. It is keyed like the sidecar, so at most one daemon uses it at a time. `launch.py` never removes it. A rootful `docker:dind` is not an option: the agent could start a `--privileged` container and get root on the host. The sidecar mounts the workdir with the same `-v` spec as the agent container and carries it in the label `pibox.docker-workdir`, which `launch.py` uses to find it; a read-only session never shares a sidecar with a read-write one. The socket lives in the named volume of the same name as the sidecar, mounted at `/pibox-docker` in both containers (outside `/run`, because `rootlesskit` copies `/run` up into its own namespace), and the agent gets `DOCKER_HOST`. Paths other than the workdir, such as `~/.pi` or `/tmp`, resolve in the sidecar, so a bind mount of them from the agent gets the sidecar's files. Containers started by the sidecar are not restricted like the agent container.

The agent container runs with `--init` (Docker's bundled `tini`); the `docker:dind-rootless` entrypoint starts its own `docker-init`. Each agent container carries the label `pibox.docker-sidecar=<sidecar name>`. `launch.py` creates the agent container with `docker create` and runs it with `docker start -a` (`-i` with a TTY) as a child process. On exit, it removes the agent container, then removes the sidecar and its volume when no other container has the label. An exclusive `flock` on `launch.py` itself (no file created) covers "find or start the sidecar, create the agent container" and "remove the agent container, count the users, remove the sidecar", so a session that exits cannot remove a sidecar that another session is about to use. A sidecar left by a killed session is reused by the next session in that workdir. It turns SIGTERM and SIGHUP into a normal exit so this cleanup still runs, because `--rm` cannot clean up when the docker CLI itself is killed.

### `--extra-package` and the AUR

`common/entrypoint.sh` installs `--extra-package` requests at container startup. It splits the list by probing `pacman -Si`: packages known to pacman install from the official repositories, everything else is treated as an AUR package. AUR packages are cloned from `aur.archlinux.org`, their repo dependencies are installed as root (parsed from `makepkg --printsrcinfo`), the package is built as the unprivileged host user (makepkg refuses to run as root), then installed with `pacman -U` (the `-debug` package is skipped).

AUR builds are not recursive: an AUR package whose own dependencies are AUR-only will fail because only official-repo dependencies are resolved. This is a deliberate simplification to avoid pulling an AUR helper and `sudo` into the image.

## Shell completions

Zsh completion lives at `completions/_pibox`. When adding, removing, or renaming a flag in `launch.py`, update `completions/_pibox` to match. The flag list is duplicated by design (no auto-generation); keep the two in sync.

Convention: list only the longest long form of each flag in the completion. Short forms (`-b`, `-p`, etc.) still work at the parser level but are omitted from the menu to reduce noise. One entry per flag, no duplicates.

`--help` / `-h` prints the argparse usage summary from `launch.py`.

## launch.py guards

`launch.py` prompts the user for confirmation before proceeding when any `--unsafe-*` option is active.

When adding new unsafe options, always add a call to the `confirm` helper to prompt the user.

`--sac-moe-patience` is an undocumented escape hatch that skips all confirmation prompts. Do not add it to the public flag table, shell completions, or help text.

## Harnesses and images

| Harness | Remote image | Local image |
|---|---|---|
| `pi` | `ghcr.io/badjware/pibox:pi` | `pibox:pi` |
| `claude` | `ghcr.io/badjware/pibox:claude` | `pibox:claude` |
| `pi-web` | `ghcr.io/badjware/pibox:pi-web` | `pibox:pi-web` |

`~/.claude` (with `~/.claude.json`) is bind-mounted only for the `claude` harness, which also gets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.

Build order when using `--build`: `Dockerfile.base` → `Dockerfile.<harness>`. `pi-web` inherits from `pibox:pi`, so `Dockerfile.pi` is built in between.

### pi-web

`pi-web` runs [Pi Web](https://github.com/agegr/pi-web) (`@agegr/pi-web`). `common/entrypoint.sh` execs `pi-web` on `0.0.0.0:30141` (set through `PI_WEB_HOSTNAME` and `PORT`). `launch.py` publishes `127.0.0.1:${PIBOX_PI_WEB_PORT:-30141}:30141` unless any `--port` is given, in which case only the user's ports are published. With `--unsafe-host-net`, it sets `PI_WEB_HOSTNAME=127.0.0.1` and `PORT=${PIBOX_PI_WEB_PORT:-30141}` instead, since there is no port mapping. Pi Web keeps its state in `~/.pi/agent`, so it needs no extra mounts. `Dockerfile.pi-web` sets `PI_WEB_NO_OPEN=1`.

### Browser

`Dockerfile.base` installs Camoufox (Firefox-based) through the npm package `camoufox`. npm installs its `playwright-core` peer dependency inside `camoufox/node_modules`. At build time, `camoufox fetch` runs with `XDG_CACHE_HOME=/opt`, so the browser and the fingerprint model ship in the image at `/opt/camoufox` and match `CAMOUFOX_VERSION`. `/opt/camoufox/browser` is a symlink to the fetched build. `CAMOUFOX_EXECUTABLE_PATH` and `CAMOUFOX_FPGEN_DATA` point to it, so Camoufox does not download anything at launch. The build deletes the GeoIP database, because Camoufox only reads it from `~/.cache/camoufox` (the `pibox-cache` volume), where it downloads on first use with `geoip: true`.

Chromium uses the same `playwright-core` copy, so the image has one Playwright version. `NODE_PATH` includes `/usr/lib/node_modules/camoufox/node_modules`, so scripts can `require("playwright-core")`. This also exposes the other camoufox dependencies. At build time, `playwright-core install chromium` downloads the Chromium build (and its headless shell) that matches this Playwright version into `PLAYWRIGHT_BROWSERS_PATH=/opt/playwright`. A `CAMOUFOX_VERSION` bump can change the Playwright version, and with it the Chromium version.

## common/image_AGENTS.md

Baked into every image as `/AGENTS.md` (and `/CLAUDE.md` for the claude image).
This is the file that agents inside the container read to understand the runtime environment.
When editing agent-facing instructions that need to persist from one container run to another, edit this file. Changes reach users only after the images are rebuilt.

## pi extensions

The host's `~/.pi` directory is bind-mounted read-write into the container, and `~/.pi/agent/extensions` read-only on top of it, so any pi extensions installed on the host (including `pi-claude-interop`, which lives in its own repo) are available inside pibox automatically. pibox itself ships no bundled extensions.

`common/entrypoint.sh` exports `BILI_CONFIG_FILE`, `BILI_SESSIONS_DIR`, and `ACP_LOG_FILE` so the `billion-context` pi package (installed under `~/.pi/agent/npm` and available through the `~/.pi` mount) keeps its state under `~/.pi/agent/billion-context/` instead of the unpersisted XDG directories. `--env` overrides them.
