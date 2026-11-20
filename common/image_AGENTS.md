## Container context
You are running inside a Docker container (based on Arch Linux).
The container is ephemeral; nothing written outside of bind-mounted paths will survive the session.

## Filesystem mounts
The following directories are bind-mounted at the same absolute path inside the container:
- **Working directory**: the host directory pibox was started from, set as the container's working directory. It may be read-only.
- **`~/.pi`**: pi agent configuration and session data, persisted across runs. `~/.pi/agent/extensions` is read-only.
- **`~/.claude`** and **`~/.claude.json`**: Claude Code agent configuration and session data, persisted across runs (claude harness only).

## Permissions
You run as a user that mirrors the host's UID, GID, and username, so files you create belong to the host user. You do not have root access, and you cannot install packages with pacman. Ask the user to relaunch pibox with `--extra-package <name>` when you need one.

## Docker
Docker may be available inside the container. When it is enabled, `DOCKER_HOST` is set.

## Display
When the user enables it, `WAYLAND_DISPLAY` is set and graphical programs such as `chromium` and `wl-clipboard` can use the host display.
