# Environment Notes

## Container context
You are running inside a Docker container (based on Ubuntu).
The container is ephemeral; nothing written outside of bind-mounted paths will survive the session.

## Filesystem mounts
The following directories are mounted in read-write mode:
- **Working directory** — the host directory from which docker was ran is bind-mounted at the same absolute path inside the container, and set as the working directory.
- **`~/.pi`** — the host user's `~/.pi` directory is bind-mounted to the same path inside the container. This is where the pi agent's configuration is persisted across runs.
- **`~/.claude`** — the host user's `~/.claude` directory is bind-mounted to the same path inside the container. This is where the Claude Code agent's configuration and session data is persisted across runs.

## User & permissions
You are running as a user that mirrors the host's UID, GID, and username. File ownership is therefore consistent between host and container. You do not have root access. **Never attempt to switch user or execute a command as a different user** (eg: executing `sudo su`).

## Available CLI tools
A short list of tools are pre-installed:
- `git`
- `node`, `npm`
- `python3` (also available as `python`)
- `go`
- `zip`, `unzip`
- `vim` (`$EDITOR` is set to `vim`)
- Standard GNU coreutils, bash utilities

Not available: `sudo`, `ssh`, `scp`, `curl`, `wget`, `grep`, `find`.

**Never** use `grep` or `find`. **Always** use `rg` instead of `grep`, and `fd` instead of `find`. These are faster, more ergonomic, and respect `.gitignore` by default.

To ensure correctness, **always** use `bc` to perform any math operations, even simple ones. **Never** generate the output yourself. In addition, **always** use `jq` to parse and manipulate JSON data and **always** use `yq` to parse and manipulate YAML data. Avoid parsing and editing these formats using regular expressions or string manipulation.

If you are unsure on how to use a command-line tool, use `man <tool>` to read its manual, or use its `--help` flag (e.g. `jq --help`) for a quick reference. Only search online if neither of those provides sufficient information.

If you require additional tools, **stop** and ask the user to install them for you. **Never** attempt to install packages yourself.

## Style

**Never** use em-dashes (—). Instead, prefer rephrasing the sentence to avoid the need for them.

Use emojies in moderation. Prefer not using them over using them too much.

When writing text destined to a human reader (eg: commit messages, code comments, README files), avoid being overly specific and overusing remarks. For example, instead of writing "this package requires python (tested on version 3.10)", write "this package requires python". This reduces the cognitive load of the reader and prevents fatigue.

There is an exception to the above rule. This style of writing is perfectly fine for text destined to an agent (eg: SKILL.md, AGENTS.md, etc.). In addition, prioritize information density over readability when writing for an agent. For example, instead of  "this package requires python (tested on version 3.10)", write "package requires python (tested on 3.10)". This is to improve token efficiency.
