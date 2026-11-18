# pibox: Project Notes

## Overview

pibox runs AI coding agents (pi and Claude Code) in an [OpenShell](https://github.com/NVIDIA/OpenShell) sandbox, using the OpenShell Docker compute driver.
`launch.py` builds a per-user image, generates a mount list, and runs `openshell sandbox create --no-keep` with the agent as the trailing command.
The working directory is mounted at the same path, and the workload runs as the host UID/GID so files keep their ownership.

Only the Docker driver is supported. Podman is deferred (see TODO).

## Repository layout

```
launch.py                           # entry point: parses flags, builds images, sets up providers and mounts, runs the sandbox
Dockerfile.base                     # shared base image (Arch Linux + tools); no entrypoint
Dockerfile.pi                       # extends base; installs @earendil-works/pi-coding-agent
Dockerfile.claude                   # extends base; installs @anthropic-ai/claude-code
Dockerfile.pi-web                   # extends pi; installs @agegr/pi-web
common/                             # files copied into images only
  image_AGENTS.md                   # baked into the image at /AGENTS.md (and /CLAUDE.md for claude)
  chromium, chrome-policies/        # chromium wrapper and policies
contrib/
  openshell/gateway.toml            # suggested gateway configuration
  openshell/policy.yaml             # suggested sandbox policy (~/.config/openshell/policy.yaml)
  systemd/                          # pi-web user units
completions/_pibox                  # zsh completion
```

`launch.py` must work when copied alone (for example to `~/.local/bin/pibox`), so it reads no file from the repo at runtime; only `--build` uses the Dockerfiles. Files it needs are embedded as strings at the end of `launch.py`:
- `USER_DOCKERFILE`: the per-user layer, built with `docker build -` (no context). It embeds the `--extra-package` installer with a `COPY <<'EOF'` heredoc.
- `AWS_ENDPOINTS_SCRIPT`: piped to `python3` in the per-user image to list the AWS SigV4 endpoints.

Files that are copied into the published images live in `common/`.

## Gateway

pibox uses the packaged OpenShell gateway (systemd user unit `openshell-gateway`) and its config `~/.config/openshell/gateway.toml`. The repo only ships a suggested base config at `contrib/openshell/gateway.toml`; the user copies or merges it. `launch.py` checks `openshell status -o json` and stops if the gateway is not connected.

Required settings: `compute_driver = "docker"`, `[openshell.drivers.docker] allow_driver_config = true` and `enable_bind_mounts = true`, and `[openshell.drivers.docker.resource_admission] enabled = false`. `sandbox_pids_limit = 1024` replaces the old `--pids-limit`.

Gateway quirks that shape `launch.py`:
- The packaged unit has `PrivateTmp=yes`, so bind sources under `/tmp` do not exist for the gateway. `launch.py` rejects them and keeps its temp files under `~/.cache/pibox/`.
- The gateway refuses a symlinked config file (`category=non_regular_path`), so the config cannot be a symlink into this repo.

## Sandbox runtime

- OpenShell replaces the image entrypoint with its own supervisor, so nothing runs as root at startup. Everything that used to be in the entrypoint moved to build time (`USER_DOCKERFILE`) or to `launch.py`.
- OpenShell rejects root workloads. `USER_DOCKERFILE` creates the host user with `-d "$HOME"` and ends with `USER <uid>:<gid>`, which the Docker driver uses when the policy has no `process` section.
- OpenShell sets `HOME=/sandbox` and `USER=<uid>`, so `launch.py` passes `HOME`, `USER`, `TERM`, `TZ`, and `COLORTERM` with `--env`. Image `ENV` is kept, and `--env` overrides it.
- The agent starts in `/sandbox` (the OpenShell workspace root); the trailing command is `bash -c 'cd -- "$1" && shift && exec "$@"' pibox <workdir> <harness command> <agent args>`.
- Without a TTY, `openshell sandbox create --no-tty` never forwards stdin EOF, so `launch.py` appends `</dev/null` to the trailing command (like the old `docker run` without `-i`).
- Sandbox names are limited to 19 characters (`pibox-<pid>`).
- `launch.py` turns SIGTERM and SIGHUP into a normal exit, and its cleanup always runs `openshell sandbox delete`, because `--no-keep` cannot clean up when the CLI itself is killed.

## Mounts

`launch.py` keeps a mount table keyed by target (defaults < user `--volume`/`--mount` < workdir; a later entry for the same target wins) and turns it into `--driver-config-json`. Landlock still applies to bind mounts, and pibox does not change the policy, so a mount target outside the policy `filesystem_policy` paths is mounted but not accessible.

- OpenShell bind mounts accept only `source`, `target`, `read_only`, and `selinux_label`, and are always `rprivate`. An `rslave` bind (the workdir, `-v ...:rslave`, `--mount bind-propagation=rslave`) becomes a Docker `local` volume with `o=rbind,rslave`, created per session and removed on exit.
- Named volumes are created with `docker volume create` first, because the driver never creates them.
- When launched from `$HOME`, the workdir is read-only unless a `--volume` for `$HOME` is given. The whole home directory is then visible, including host credentials and dotfiles such as `~/.bashrc` (kept on purpose).
- `--read-only` applies to the workdir only. pi needs `~/.pi` writable.

## Network policy

pibox passes one policy file to `openshell sandbox create --policy`, unchanged: `--policy`, else `$OPENSHELL_SANDBOX_POLICY`, else `~/.config/openshell/policy.yaml` (`DEFAULT_POLICY`). It stops when the file does not exist. The user owns the file; `contrib/openshell/policy.yaml` is the suggested start, with absolute paths because YAML expands nothing.

The suggested policy allows `~/develop` and the default mount targets, and uses Landlock `best_effort`: with `hard_requirement`, OpenShell stops the sandbox when a listed path does not exist (for example `~/.claude` with the pi harness, or `~/develop` when it is not mounted).

The credential rules are not in the policy file. AWS and gcx rules come from endpoint-bearing provider profiles (OpenShell adds `_provider_*` rules for attached providers). The kube rule is added at runtime with `openshell policy update` (`add_kube_rule`).

OpenShell takes one base policy per sandbox and never writes back to it. Rules added at runtime (`openshell policy update`, advisor approvals) die with the sandbox. OpenShell 0.1.8 has no `openshell policy lint`; test a policy with a real sandbox.

A gateway-global policy (`openshell policy set --global`) is not usable: it replaces every sandbox policy and suppresses provider rules.

Policy facts worth knowing:
- A rule also applies to the processes that a listed binary starts, so `/usr/bin/node` (pi) and the native `claude.exe` cover the tools the agent runs.
- Wildcard hosts need three labels; `**` is allowed only as the whole first label.
- Exact hostnames can reach private IPs; wildcard and hostless endpoints need `allowed_ips`.
- Endpoints that overlap on a port must have the same metadata. For example, `*.example.com` with `allowed_ips` and `foo.example.com` without it are rejected (`network endpoint ambiguity validation failed`).
- OpenShell blocks every connection upgrade on `protocol: rest` endpoints except through `protocol: websocket` endpoints, and it drops `kubectl port-forward` upgrades in every configuration tested.

## Credentials

AWS, gcx, and kube activate automatically when their host configuration exists. None of them puts a real credential in the sandbox.

- **AWS** (`setup_aws`): stores every profile from `~/.aws/credentials` in its own provider `pibox-aws-<profile>` (profile type `pibox-aws`), and attaches only the one named by the host `AWS_PROFILE` (default `default`). A sandbox cannot hold more than one AWS provider: the signer reads the fixed `AWS_*` keys and OpenShell rejects two attached providers with the same key. `AWS_ENDPOINTS_SCRIPT` runs in the per-user image and lists one SigV4 endpoint per AWS service from the bundled botocore models; they go into the `pibox-aws` profile with `credential_signing: sigv4`, so the attached provider adds their rule. Only the image's Python interpreter may reach them. The sandbox sees placeholder `AWS_*` variables and the proxy re-signs requests, so IAM controls access. Credentials are assumed not to rotate during a session.
- **gcx** (`setup_gcx`): stores each stack token from `~/.config/gcx/config.yaml` in its own provider `pibox-gcx-<stack>` with its own profile of the same name (one bearer credential `GRAFANA_TOKEN`), and attaches only the stack of `current-context`. Each profile lists the Grafana host of its stack as its endpoint, because endpoints belong to the profile, with `/usr/local/bin/gcx` and `~/go/bin/gcx` as the only binaries (`USER_DOCKERFILE` puts `~/go/bin` first in `PATH`, so a mounted `~/go` shadows the image gcx). The sandbox gets no gcx config file: gcx reads the placeholder `GRAFANA_TOKEN` and the `GRAFANA_SERVER`, `GRAFANA_ORG_ID`, and `GRAFANA_STACK_ID` variables set from that stack. When the workdir is `$HOME`, gcx also loads the host config, and the variables override its current context.
- **kube** (`setup_kube`): starts one `kubectl proxy --port 0` per host context on `127.0.0.1`, accepting only the host `host.openshell.internal` and allowing `exec`/`attach` (`--reject-paths '^$'`). The sandbox kubeconfig (`/etc/pibox/kubeconfig`, `KUBECONFIG`) has no credentials and points at `http://host.openshell.internal:<port>`. The ports change with each session, so a background thread (`add_kube_rule`) runs `openshell policy update --wait` once the sandbox exists, with one `--add-endpoint` per port for `/usr/bin/kubectl`. `--rule-name` accepts only one endpoint, so OpenShell names the rules. `kubectl` fails until the rule loads. The proxies stop at the end of the session. `port-forward` does not work.

Provider profiles and providers stay in the gateway between sessions; each launch overwrites them. Credential values reach the `openshell` CLI through its environment, not its arguments.

## launch.py flags

| Flag | Short | Description |
|---|---|---|
| `--help` | `-h` | show usage help and exit |
| `--harness pi\|claude\|pi-web` | `-H` | agent to run (default: `pi`) |
| `--build` | | build images locally instead of pulling |
| `--pull` | | pull latest image before launch |
| `--unsafe-enable-docker` | (none, intentional) | start a rootless dockerd in a privileged sidecar container and share its socket |
| `--unsafe-host-wayland` | (none, intentional) | mount the Wayland socket into the sandbox and forward Wayland env vars |
| `--ephemeral`, `--tmp` | | use a temp workdir under `~/.cache/pibox/` |
| `--workdir` | `-w` | use the given directory as the workdir instead of the current one (incompatible with `--ephemeral`) |
| `--read-only`, `--ro` | `-r` | mount the workdir read-only |
| `--volume` | `-v` | mount a volume (repeatable; Docker `-v` syntax with options `ro`, `rw`, `rslave`, `slave`, `private`, `rprivate`; `PATH` or `PATH:ro\|rw` mounts `PATH` at the same absolute path) |
| `--mount` | `-m` | add a mount (repeatable; Docker `--mount` syntax with `type`, `src`, `dst`, `readonly`, `bind-propagation`, `tmpfs-size`) |
| `--env` | `-e` | set an environment variable (repeatable; Docker `-e` syntax; `VAR` alone copies the host value; overrides variables set by pibox) |
| `--extra-package` | `-P` | install an extra pacman or AUR package in the per-user image (repeatable) |
| `--port` | `-p` | forward a sandbox port to the same host port (repeatable; `[IP:]PORT`, or Docker `-p` syntax with equal ports) |
| `--policy` | | OpenShell policy file to use instead of `$OPENSHELL_SANDBOX_POLICY` or `~/.config/openshell/policy.yaml` |
| `--` | | remaining args forwarded to the agent inside the sandbox |

Unsafe options (`--unsafe-*`) intentionally have no short form to reduce the risk of accidental use.

### `--extra-package` and the AUR

`USER_DOCKERFILE` runs the embedded `install-extra-packages.sh` at build time. It runs `pacman -Syu` first (Arch does not support partial upgrades), then splits the list by probing `pacman -Si`: packages known to pacman install from the official repositories, everything else is treated as an AUR package. AUR packages are cloned from `aur.archlinux.org`, their repo dependencies are installed as root (parsed from `makepkg --printsrcinfo`), the package is built as the host user (makepkg refuses to run as root), then installed with `pacman -U` (the `-debug` package is skipped).

AUR builds are not recursive: an AUR package whose own dependencies are AUR-only will fail. Packages that verify a PGP signature also fail, because the build user has no keys.

### `--port` and port forwards

OpenShell forwards a sandbox port to the same host port, and `--forward` cannot be combined with `--no-keep`. `launch.py` starts each forward with `openshell forward start -d` from a background thread that checks `openshell forward list -o json` every second and restarts dead forwards (a forward exits when nothing listens yet).

### `--unsafe-enable-docker`

OpenShell sandboxes cannot run a Docker daemon (non-root, seccomp, no `mount`). `launch.py` starts a privileged sidecar `pibox-<pid>-docker` from the per-user image running `dockerd-rootless.sh` as the host user, with the workdir mounted at the same path (`rslave`). The socket lives in `/pibox-docker`, outside `/run` because `rootlesskit` copies `/run` up into its own namespace, and is bind-mounted into the sandbox at the same path with `DOCKER_HOST` set. `USER_DOCKERFILE` adds the `/etc/subuid`/`/etc/subgid` entries and `setcap` on `newuidmap`/`newgidmap` for it. Containers started by the sidecar are not subject to the sandbox network policy.

## Shell completions

Zsh completion lives at `completions/_pibox`. When adding, removing, or renaming a flag in `launch.py`, update `completions/_pibox` to match. The flag list is duplicated by design (no auto-generation); keep the two in sync.

Convention: list only the longest long form of each flag in the completion. Short forms (`-b`, `-p`, etc.) and shorter long-form aliases (e.g. `--tmp` for `--ephemeral`, `--ro` for `--read-only`) still work at the parser level but are omitted from the menu to reduce noise. One entry per flag, no duplicates.

`--help` / `-h` prints the argparse usage summary from `launch.py`.

## launch.py guards

`launch.py` prompts the user for confirmation before proceeding in the following cases:
- running as root
- any `--unsafe-*` option is active
- any `--port` is given

When adding new unsafe options, always add a call to the `confirm` helper to prompt the user.

`--sac-moe-patience` is an undocumented escape hatch that skips all confirmation prompts. Do not add it to the public flag table, shell completions, or help text.

## Harnesses and images

| Harness | Remote image | Local image | Command |
|---|---|---|---|
| `pi` | `ghcr.io/badjware/pibox:pi` | `pibox:pi` | `pi` |
| `claude` | `ghcr.io/badjware/pibox:claude` | `pibox:claude` | `claude --dangerously-skip-permissions` |
| `pi-web` | `ghcr.io/badjware/pibox:pi-web` | `pibox:pi-web` | `pi-web` |

Every launch builds `pibox-user:<harness>` from `USER_DOCKERFILE` on top of the harness image (Docker caches it).

`~/.claude` (with `~/.claude.json`) is mounted only for the `claude` harness, which also gets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.

Build order when using `--build`: `Dockerfile.base` → `Dockerfile.<harness>`. `pi-web` inherits from `pibox:pi`, so `Dockerfile.pi` is built in between.

### pi-web

`pi-web` runs [Pi Web](https://github.com/agegr/pi-web) (`@agegr/pi-web`) with `PI_WEB_HOSTNAME=127.0.0.1`. Without `--port`, it listens on `PORT=${PIBOX_PI_WEB_PORT:-30141}` and `launch.py` forwards `127.0.0.1:<same port>`. With `--port`, it listens on 30141 and only the user's ports are forwarded. Pi Web keeps its state in `~/.pi/agent`, so it needs no extra mounts. `Dockerfile.pi-web` sets `PI_WEB_NO_OPEN=1`.

## common/image_AGENTS.md

Baked into every image as `/AGENTS.md` (and `/CLAUDE.md` for the claude image).
This is the file that agents inside the sandbox read to understand the runtime environment.
When editing agent-facing instructions that need to persist from one run to another, edit this file. Changes reach users only after the images are rebuilt.

## pi extensions

The host's `~/.pi/agent/extensions` directory is mounted read-only into the sandbox, so any pi extensions installed on the host (including `pi-claude-interop`, which lives in its own repo) are available inside pibox automatically. pibox itself ships no bundled extensions.

`USER_DOCKERFILE` sets `BILI_CONFIG_FILE`, `BILI_SESSIONS_DIR`, and `ACP_LOG_FILE` so the `billion-context` pi package (installed under `~/.pi/agent/npm` and available through the `~/.pi` mount) keeps its state under `~/.pi/agent/billion-context/` instead of the unpersisted XDG directories. `--env` overrides them.

## TODO

1. Move Wayland to `waypipe` over SSH (`openshell ssh-proxy`) instead of mounting the socket.
2. Split `~/.pi` and `~/.claude` into copied configuration, read-write state directories only, and providers for secrets. Today a sandboxed agent can change code and instructions that the host pi loads (`npm/`, `packages/`, `git/`, `settings.json`, `skills/`, `AGENTS.md`).
3. Install fonts in the base image and remove the font mounts.
4. Support the Podman driver (needs subordinate UID/GID ranges for the host user).
5. Before deleting the sandbox, print the network rules added during the session so the user can copy them into the policy file.
6. Serve `kubectl proxy` over a Unix socket mounted into the sandbox (with a TCP forwarder inside) instead of loopback ports that any local process can use.
