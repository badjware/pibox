# Plan: add Claude Code support to pibox

Status: draft for review.

## 1. Goals & non-goals

**Goals**
- Run Anthropic's Claude Code CLI (`@anthropic-ai/claude-code`) inside the same
  pibox sandbox as `pi`.
- Ship pi and Claude Code as **separate images built from a shared base**, so
  they can be versioned, pulled, and cached independently.
- Let the user pick the agent per-invocation with a `--harness pi|claude` flag
  on `launch.sh`; default stays `pi`.
- Persist Claude Code auth/session across runs the same way `~/.pi` is today.

**Non-goals**
- Running both agents in the same container at the same time.
- Replacing `pi` or changing its defaults.
- Shipping Anthropic credentials in the image.
- Adding a Claude-specific config templating flow (dropped per review).

## 2. Image layout

Split the current single `Dockerfile` into one shared base + two leaves:

```
Dockerfile.base      # OS + tooling + entrypoint + AGENTS.md
Dockerfile.pi        # FROM base; installs pinned @mariozechner/pi-coding-agent
Dockerfile.claude    # FROM base; installs pinned @anthropic-ai/claude-code
```

- `Dockerfile.base` = current `Dockerfile` minus the
  `npm install -g @mariozechner/pi-coding-agent` step. It keeps `tini`, the apt
  package list, the `image_AGENTS.md` → `/AGENTS.md` copy, `entrypoint.sh`, and
  the `ENTRYPOINT`.
- `Dockerfile.pi` and `Dockerfile.claude` each consist of a single
  `ARG …_VERSION=x.y.z` + `npm install -g …@${VERSION}` layer.
- The Claude leaf additionally `COPY`s `image_AGENTS.md` to `/CLAUDE.md` so
  Claude Code picks the guidance file up automatically. We copy (rather than
  symlink to `/AGENTS.md`) so Claude Code does not read the same file twice.
- `CLAUDE_CODE_VERSION` is **pinned** to an exact released version. The value
  used at implementation time is the latest stable release of
  `@anthropic-ai/claude-code` on npm at that moment, matching the
  `PI_CODING_AGENT_VERSION` style.

### Dockerfile sketches

`Dockerfile.pi`:
```dockerfile
ARG BASE_IMAGE=pibox-base:latest
FROM ${BASE_IMAGE}
ARG PI_CODING_AGENT_VERSION=0.73.1
RUN npm install -g @mariozechner/pi-coding-agent@${PI_CODING_AGENT_VERSION}
```

`Dockerfile.claude`:
```dockerfile
ARG BASE_IMAGE=pibox-base:latest
FROM ${BASE_IMAGE}
ARG CLAUDE_CODE_VERSION=<pinned>
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
COPY content/image_AGENTS.md /CLAUDE.md
```

### Published tags (GHCR)

All tags live under a single `ghcr.io/badjware/pibox` repo. The base image is
**not** published; it only exists as a local build stage / local cache layer.

- `ghcr.io/badjware/pibox:pi`
- `ghcr.io/badjware/pibox:claude`
- `ghcr.io/badjware/pibox:latest` — alias of `pibox:pi` for one release cycle,
  then dropped with a changelog note.

## 3. `launch.sh` changes

1. Add a long option `--harness pi|claude`, default `pi`.
2. Select the image from the harness:
   ```bash
   case "$harness" in
     pi)     REMOTE_IMAGE="ghcr.io/badjware/pibox:pi"
             LOCAL_IMAGE="pibox:pi" ;;
     claude) REMOTE_IMAGE="ghcr.io/badjware/pibox:claude"
             LOCAL_IMAGE="pibox:claude" ;;
     *)      echo "unknown --harness: $harness" >&2; exit 2 ;;
   esac
   ```
3. `--build` builds the matching leaf image locally:
   ```bash
   docker build --pull \
     -f "$SCRIPT_DIR/Dockerfile.$harness" \
     -t "$LOCAL_IMAGE" "$SCRIPT_DIR"
   ```
   (The leaf Dockerfiles either `FROM` a base tag that `--build` builds first,
   or we use a single multi-stage file per leaf that starts
   `FROM ubuntu:resolute AS base` to avoid requiring a separate base build.)
4. Pass `HARNESS` into the container so the entrypoint can dispatch:
   ```bash
   -e "HARNESS=$harness"
   ```
5. Flip the `~/.claude` bind mount to **rw** unconditionally (drop the current
   `:ro`).
6. Ephemeral behavior:
   - `--harness pi` keeps `--no-session` (unchanged).
   - `--harness claude` under `--ephemeral` mounts a tmpfs over `~/.claude` so
     session/project state does not leak to the host.
7. Forward Claude-relevant env vars unconditionally, the same way
   `ANTHROPIC_AUTH_TOKEN` is forwarded today:
   - `ANTHROPIC_API_KEY`
   - `CLAUDE_CODE_OAUTH_TOKEN`
   - `ANTHROPIC_BASE_URL`
   - `ANTHROPIC_MODEL`
8. `--config-tmpl` is left untouched (still renders pi's `models.json`). No
   Claude templating is added.

## 4. `entrypoint.sh` changes

1. Read `HARNESS="${HARNESS:-pi}"`.
2. Add a `~/.claude` ownership fix-up next to the existing `.local` stub loop,
   since it is now rw.
3. Replace the final exec line with a dispatch:
   ```bash
   case "$HARNESS" in
     pi)     exec runuser -u "$HOST_USER" -- pi "$@" ;;
     claude) exec runuser -u "$HOST_USER" -- claude "$@" ;;
     *)      echo "unknown HARNESS: $HARNESS" >&2; exit 2 ;;
   esac
   ```

The entrypoint lives in `Dockerfile.base` and is shared. The unreachable branch
in each leaf image is harmless and avoids maintaining two entrypoints.

## 5. Guidance file (AGENTS.md / CLAUDE.md)

- `content/image_AGENTS.md` remains the single source of truth. The base image
  copies it to `/AGENTS.md`.
- The Claude leaf additionally copies the same file to `/CLAUDE.md` (plain
  `COPY`, no symlink) to avoid Claude Code reading it twice.
- Light wording pass on the file so it reads "the agent" rather than being
  pi-specific.

## 6. Security review

- Claude Code runs under the same dropped-caps / `no-new-privileges` profile;
  no changes required there.
- The only new host write surface is `~/.claude` flipping to rw, which mirrors
  how `~/.pi` is already handled. Called out in the README.
- DinD path is agent-agnostic; verified both harnesses still launch under
  `--privileged`.

## 7. README updates

1. Mention the two images and the `--harness` flag in Quick start; default
   stays pi.
2. New subsection **"Running Claude Code"**:
   ```sh
   ./launch.sh --harness claude
   ./launch.sh --harness claude -- -p "summarize the README"
   ```
3. Document a `claudebox` alias alongside the existing `pibox` one:
   ```sh
   alias pibox='/path/to/pibox/launch.sh --harness pi'
   alias claudebox='/path/to/pibox/launch.sh --harness claude'
   ```
4. Update the bind-mounts table: `~/.claude` is now `rw`.
5. Document the env vars forwarded to the Claude harness.
6. Note that `--config-tmpl` only applies to the pi harness.

## 8. CI / release

1. Publish workflow builds and pushes two image tags: `pibox:pi` and
   `pibox:claude`. The base stage is built as a local layer only (not
   published). The two leaves can build in parallel once the base stage is
   cached.
2. Bump rules:
   - `PI_CODING_AGENT_VERSION` bump → rebuild `pibox:pi` only.
   - `CLAUDE_CODE_VERSION` bump → rebuild `pibox:claude` only.
   - Base package bump → rebuild both.
3. Smoke tests per image:
   - `docker run --rm ghcr.io/badjware/pibox:pi pi --version`
   - `docker run --rm -e HARNESS=claude ghcr.io/badjware/pibox:claude claude --version`
4. Keep `pibox:latest` as an alias of `pibox:pi` for one release, then remove.

## 9. Rollout order

Each step lands as a single commit on its own branch (no PR framing).

1. **Split the Dockerfile**: introduce `Dockerfile.base` and `Dockerfile.pi`,
   wire `launch.sh --build` and CI to them, publish `pibox:pi`. No
   user-visible change.
2. **Claude image**: add `Dockerfile.claude` with pinned `CLAUDE_CODE_VERSION`
   and the `/CLAUDE.md` copy, CI publish of `pibox:claude`.
3. **Harness dispatch**: `HARNESS` env + entrypoint dispatch +
   `launch.sh --harness` + rw `~/.claude` + ephemeral tmpfs handling +
   unconditional forwarding of the extra Anthropic env vars.
4. **Docs**: README updates, including the `claudebox` alias.
