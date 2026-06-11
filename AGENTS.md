# pibox: Project Notes

## Overview

pibox wraps AI coding agents (pi, Claude Code) inside a sandboxed Docker container.
The working directory is bind-mounted into the container so the agent operates on host files with matching UID/GID ownership.

## Repository layout

```
launch.sh                           # main entry point: parses flags, builds/pulls image, runs container
Dockerfile.base                     # shared base image (Ubuntu + tools)
Dockerfile.pi                       # extends base; installs @earendil-works/pi-coding-agent and pi-acp
Dockerfile.claude                   # extends base; installs @anthropic-ai/claude-code
content/
  image_AGENTS.md                   # AGENTS.md baked into the image at /AGENTS.md (and /CLAUDE.md for claude)
  entrypoint.sh                     # container entrypoint
```

## launch.sh flags

| Flag | Short | Description |
|---|---|---|
| `--help` | `-h` | show usage help and exit |
| `--harness pi\|claude` | `-H` | agent to run (default: `pi`) |
| `--build` | `-b` | build images locally instead of pulling |
| `--pull` | `-p` | pull latest image before launch |
| `--unsafe-enable-docker` | (none, intentional) | enable rootless Docker-in-Docker (privileged) |
| `--unsafe-enable-aws` | (none, intentional) | mount `~/.aws` into the container (awscli is pre-installed) |
| `--unsafe-enable-kube` | (none, intentional) | mount `~/.kube` into the container (kubectl is pre-installed) |
| `--unsafe-host-wayland` | (none, intentional) | mount the Wayland socket into the container and forward Wayland env vars |
| `--unsafe-host-net` | (none, intentional) | share the host network namespace (`--network=host`) |
| `--ephemeral`, `--tmp` | `-e` | use a temp workdir |
| `--read-only`, `--ro` | `-r` | mount all volumes read-only |
| `--volume` | `-v` | bind-mount an extra volume (repeatable; same syntax as `docker run -v`) |
| `--extra-package` | `-P` | install an extra apt package at container startup (repeatable; non-persistent) |
| `--acp` | (none) | run the `pi-acp` adapter instead of `pi`, exposing ACP (JSON-RPC 2.0 over stdio) for editors like Zed. Implies `-i` (no TTY) on `docker run`. Only valid with `--harness pi`. |
| `--` | | remaining args forwarded to the agent inside the container |

Unsafe options (`--unsafe-*`) intentionally have no short form to reduce the risk of accidental use.

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

Build order when using `--build`: `Dockerfile.base` → `Dockerfile.<harness>`.

## content/image_AGENTS.md

Baked into every image as `/AGENTS.md` (and `/CLAUDE.md` for the claude image).
This is the file that agents inside the container read to understand the runtime environment.
When editing agent-facing instructions that need to persist from one container run to another, edit this file.

## pi extensions

The host's `~/.pi/agent/extensions` directory is bind-mounted into the container, so any pi extensions installed on the host (including `pi-claude-interop`, which lives in its own repo) are available inside pibox automatically. pibox itself no longer ships any bundled extensions.

## Environment variables forwarded into the container

`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
`ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`
