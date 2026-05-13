# pibox: Project Notes

## Overview

pibox wraps AI coding agents (pi, Claude Code) inside a sandboxed Docker container.
The working directory is bind-mounted into the container so the agent operates on host files with matching UID/GID ownership.

## Repository layout

```
launch.sh                           # main entry point: parses flags, builds/pulls image, runs container
Dockerfile.base                     # shared base image (Ubuntu + tools)
Dockerfile.pi                       # extends base; installs @earendil-works/pi-coding-agent
Dockerfile.claude                   # extends base; installs @anthropic-ai/claude-code
content/
  image_AGENTS.md                   # AGENTS.md baked into the image at /AGENTS.md (and /CLAUDE.md for claude)
  entrypoint.sh                     # container entrypoint
pi/
  extensions/
    pi-claude-interop/
      index.ts                      # pi extension: bridges Claude Code assets (commands, skills, rules) to pi
      models.json.tmpl              # provider/model config template rendered with process.env at runtime
```

## launch.sh flags

| Flag | Short | Description |
|---|---|---|
| `--harness pi\|claude` | `-H` | agent to run (default: `pi`) |
| `--build` | `-b` | build images locally instead of pulling |
| `--pull` | `-p` | pull latest image before launch |
| `--unsafe-enable-docker` | (none, intentional) | enable rootless Docker-in-Docker (privileged) |
| `--ephemeral`, `--tmp` | `-e` | use a temp workdir; for pi, also passes `--no-session` |
| `--read-only`, `--ro` | `-r` | mount all volumes read-only |
| `--` | | remaining args forwarded to the agent inside the container |

Unsafe options (`--unsafe-*`) intentionally have no short form to reduce the risk of accidental use.

## Harnesses and images

| Harness | Remote image | Local image |
|---|---|---|
| `pi` | `ghcr.io/badjware/pibox:pi` | `pibox:pi` |
| `claude` | `ghcr.io/badjware/pibox:claude` | `pibox:claude` |

Build order when using `--build`: `Dockerfile.base` → `Dockerfile.<harness>`.

## content/image_AGENTS.md

Baked into every image as `/AGENTS.md` (and `/CLAUDE.md` for the claude image).
This is the file that agents inside the container read to understand the runtime environment.
When editing agent-facing instructions, edit this file.

## pi-claude-interop extension

Mounted read-only at `~/.pi/agent/extensions/pi-claude-interop` inside the container.
Bridges Claude Code assets to pi at runtime:
- `.claude/commands/**/*.md` and `~/.claude/commands/**/*.md` → pi prompt templates
- `.claude/skills/` and `~/.claude/skills/` → pi skill paths
- `.claude/rules/*.md` → injected into pi system prompt
- `models.json.tmpl` → rendered with env vars, registered as a pi provider

When modifying the extension, edit `pi/extensions/pi-claude-interop/index.ts`.
The extension is TypeScript; compile if a build step is required.

## Environment variables forwarded into the container

`ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
`ANTHROPIC_CUSTOM_HEADERS`, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`
