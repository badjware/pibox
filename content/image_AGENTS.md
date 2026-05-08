# Environment Notes

## Container context
You are running inside a Docker container (based on Ubuntu).
The container is ephemeral; nothing written outside of bind-mounted paths will survive the session.

## Filesystem mounts
- **Working directory** — the host directory from which docker was ran is bind-mounted at the same absolute path inside the container, and set as the working directory. All file edits here are persisted.
- **`~/.pi`** — the host user's `~/.pi` directory is bind-mounted to the same path inside the container. This is where pi's configuration is persisted across runs.

## User & permissions
You are running as a user that mirrors the host's UID, GID, and username. File ownership is therefore consistent between host and container. You do not have root access. **Never attempt to switch user or execute a command as a different user** (eg: executing `sudo su`).

## Available CLI tools
The following tools are pre-installed and available on `PATH`:
- `git`
- `node`, `npm`
- `python3` (also available as `python`)
- `go`
- `fd` (also available as `fdfind`, always use it over `find`)
- `rg` (always use it over `grep`)
- `jq` (JSON processor)
- `yq` (`jq` wrapper for YAML files)
- `bc` (arbitrary-precision calculator)
- `zip`, `unzip`
- `vim` (`$EDITOR` is set to `vim`)
- Standard GNU coreutils, bash utilities

Not available: `sudo`, `ssh`, `scp`, `curl`, `wget`, `man`, `docker`.

To ensure correctness, **always** use `bc` to perform any math operations, even simple ones. **Never** generate the output yourself.

If you are unsure on how to use any of the tools, use their `--help` flag (e.g. `jq --help`) for a quick reference. If this fails or is insufficient, search online for their `man` page, usage examples, and documentation.

If you require additional tools, **stop** and ask the user to install them for you. **Never** attempt to install packages yourself.
