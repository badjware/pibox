# Environment Notes

## Container context
You are running inside a Docker container (based on Ubuntu).
The container is ephemeral: nothing written outside of bind-mounted paths will survive the session.

## Filesystem mounts
- **Working directory** — the host directory from which `launch.sh` was invoked is bind-mounted at the same absolute path inside the container, and set as the working directory. All file edits here are persisted.
- **`~/.pi`** — the host user's `~/.pi` directory is bind-mounted to the same path inside the container. This is where pi's configuration (including `agent/models.json`) is persisted across runs.

## User & permissions
The entrypoint creates a user inside the container that mirrors the host's UID, GID, and username, then drops privileges before launching `pi`. File ownership is therefore consistent between host and container; you do not need to `sudo` for normal file operations.

## Available CLI tools
The following tools are pre-installed and available on `PATH`:
- `node`, `npm`
- `python3` (also available as `python`)
- `fd` (prefer using it over `find`)
- `rg` (prefer using it over `grep`)
- `jq` (JSON processor)
- `bc` (arbitrary-precision calculator)
- `zip`, `unzip`
- `vim` (`$EDITOR` is set to `vim`)
- Standard GNU coreutils, bash utilities

**Not available:** `ssh`, `scp`, `curl`, `wget`, `docker`.
