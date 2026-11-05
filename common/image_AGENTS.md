## Container context
You are running inside a Docker container (based on Arch Linux).
The container is ephemeral; nothing written outside of bind-mounted paths will survive the session.

## Filesystem mounts
The following directories are bind-mounted read-write by default at the same absolute path inside the container:
- **Working directory**: the host directory from which docker was run, set as the container's working directory.
- **`~/.pi`**: pi agent configuration and session data, persisted across runs.
- **`~/.claude`**: Claude Code agent configuration and session data, persisted across runs (claude harness only).

## Permissions & safety
You are running as a user that mirrors the host's UID, GID, and username, so file ownership is consistent between host and container. You do not have root access.

## Docker
Docker may be available inside the container. When it is enabled, `DOCKER_HOST` is set.
