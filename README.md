# pibox

pibox runs AI coding agents in a sandbox, on your working directory.

It starts the agent in an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox, an Arch Linux container with a policy for files and network access.
The agent runs as your host user, so the files it writes in your working
directory belong to you. It cannot use root, and it reaches only the hosts
that the policy allows.

Three harnesses are supported:

| Harness        | Agent                                                      | Image                            |
| -------------- | ---------------------------------------------------------- | -------------------------------- |
| `pi` (default) | [pi](https://github.com/earendil-works/pi)                 | `ghcr.io/badjware/pibox:pi`      |
| `claude`       | [Claude Code](https://github.com/anthropics/claude-code)   | `ghcr.io/badjware/pibox:claude`  |
| `pi-web`       | [Pi Web](https://github.com/agegr/pi-web), a web UI for pi | `ghcr.io/badjware/pibox:pi-web`  |

## How it works

At each launch, `launch.py`:

1. Pulls or builds the harness image.
2. Builds a small per-user image on top of it. This image
   contains your user with the same UID, GID, and home path, and the packages
   you request with `--extra-package`. Docker caches it.
3. Sets up credentials for AWS, Grafana (`gcx`), and Kubernetes when you have
   them on the host. The real credentials stay outside the sandbox.
4. Generates the mount list.
5. Runs `openshell sandbox create` with the agent, and deletes the sandbox when
   the agent exits.

## Requirements

- Docker
- OpenShell, with its gateway running on the Docker driver (see below)
- Python 3 with PyYAML (`python3-pyyaml` on Fedora, `python-yaml` on Arch)
- Optional: `kubectl` and the AWS CLI on the host, for Kubernetes access

pibox is tested on Linux only.

## Setup

Install OpenShell, then configure its gateway. The packaged gateway reads
`~/.config/openshell/gateway.toml`. Copy the suggested configuration, or merge
it into yours, then restart the gateway:

```sh
cp contrib/openshell/gateway.toml ~/.config/openshell/gateway.toml
systemctl --user restart openshell-gateway
openshell status
```

The configuration selects the Docker driver and turns on the bind mounts that
pibox needs. Every sandbox on this gateway can then use bind mounts.

Copy the suggested sandbox policy, then replace `/home/user` with your home
directory and `1000` with your UID:

```sh
cp contrib/openshell/policy.yaml ~/.config/openshell/policy.yaml
```

See [Policy](#policy) for what it allows.

Put `launch.py` in your `PATH`. It is self-contained, so a copy works:

```sh
git clone https://github.com/badjware/pibox.git
cp pibox/launch.py ~/.local/bin/pibox
```

Only `--build` needs a clone of the repository, for the Dockerfiles.

Set aliases for the other harnesses if you like:

```sh
alias claudebox='pibox --harness claude'
```

## Usage

```
pibox [options] [-- <agent args>]
```

The directory you run pibox from becomes the working directory of the agent.

### Flags

| Flag                           | Short | Description                                                                                                         |
| ------------------------------ | ----- | ------------------------------------------------------------------------------------------------------------------- |
| `--help`                       | `-h`  | Show usage help and exit.                                                                                           |
| `--harness pi\|claude\|pi-web` | `-H`  | Choose the agent to run. Defaults to `pi`.                                                                          |
| `--build`                      |       | Build the images locally from the Dockerfiles instead of using the published images.                                |
| `--pull`                       |       | Update the image before launching.                                                                                  |
| `--ephemeral`, `--tmp`         |       | Start in a temporary working directory instead of the current one.                                                  |
| `--workdir <dir>`              | `-w`  | Use `<dir>` as the working directory. Incompatible with `--ephemeral`.                                              |
| `--read-only`, `--ro`          | `-r`  | Mount the working directory read-only.                                                                              |
| `--volume <spec>`              | `-v`  | Mount a volume. Repeatable, `docker run -v` syntax. `PATH` or `PATH:ro\|rw` mounts `PATH` at the same path.         |
| `--mount <spec>`               | `-m`  | Add a mount. Repeatable, `docker run --mount` syntax.                                                               |
| `--env <var>[=<value>]`        | `-e`  | Set an environment variable. Repeatable, `docker run -e` syntax.                                                    |
| `--extra-package <name>`       | `-P`  | Install an extra pacman or AUR package in the per-user image. Repeatable.                                           |
| `--port <spec>`                | `-p`  | Forward a sandbox port to the same port on the host. Repeatable, `[IP:]PORT`.                                       |
| `--policy <file>`              |       | Use this policy file instead of `$OPENSHELL_SANDBOX_POLICY` or `~/.config/openshell/policy.yaml`.                   |
| `--unsafe-enable-docker`       |       | Start a rootless Docker daemon in a privileged container next to the sandbox, and let the agent use it.             |
| `--unsafe-host-wayland`        |       | Mount the Wayland socket into the sandbox and forward the Wayland environment variables.                            |

Any arguments after `--` go to the agent.

`--volume` accepts the options `ro`, `rw`, `rslave`, `slave`, `private`, and
`rprivate`. `--mount` accepts `type` (`bind`, `volume`, `tmpfs`), `src`, `dst`,
`readonly`, `bind-propagation`, and `tmpfs-size`. pibox rejects other options.
Bind sources cannot be under `/tmp`, because the gateway runs with a private
`/tmp`.

### Examples

Launch pi:

```sh
pibox
```

Launch Claude Code:

```sh
pibox --harness claude
```

Launch Pi Web and open http://127.0.0.1:30141:

```sh
pibox --harness pi-web
```

Pass arguments to the agent:

```sh
pibox -- -p "summarize the README"
```

Install extra packages:

```sh
pibox -P tree -P openscad-git
```

Packages known to pacman come from the official repositories. pibox treats
anything else as an AUR package and builds it from source. AUR builds are not
recursive, so an AUR package with AUR-only dependencies fails to build, and so
does a package that checks a PGP signature. The build runs once and Docker
caches it until the package list or the image changes.

## Policy

pibox gives OpenShell one policy file, and does not change it. It uses the
first of:

1. `--policy <file>`
2. `$OPENSHELL_SANDBOX_POLICY`
3. `~/.config/openshell/policy.yaml`

pibox stops when the file does not exist. The suggested policy in
`contrib/openshell/policy.yaml` allows:

- Files: the system paths, `~/develop`, and the paths that pibox mounts by
  default. Landlock blocks everything else, including mounts that are not in
  the policy. A working directory outside `~/develop` is mounted, but the agent
  cannot read it. Add the paths you mount with `--volume` or `--mount`.
- Network: the model endpoints. Everything else fails: package registries,
  GitHub, web pages.

A network rule lists the hosts and the programs that can reach them:

```yaml
network_policies:
  github:
    endpoints:
      - { host: github.com, port: 443 }
      - { host: api.github.com, port: 443 }
    binaries:
      - { path: /usr/bin/node }
```

A rule also applies to the processes that a listed program starts, so
`/usr/bin/node` covers the tools that pi runs. See the
[OpenShell policy reference](https://docs.nvidia.com/openshell/how-it-works/policies/schema)
for the format.

The AWS, Grafana, and Kubernetes rules come from pibox (see
[Credentials](#credentials)), so the policy file does not need them.

Rules that you approve while the sandbox runs, for example with
`openshell policy update`, disappear with the sandbox. Put the rules you keep
in your policy file.

## Credentials

pibox sets up these tools automatically when their configuration exists on the
host. The agent uses them normally, but it never sees a real credential.

**AWS.** pibox stores each profile from `~/.aws/credentials` in its own
OpenShell provider, and gives the sandbox the profile named by `AWS_PROFILE`
(or `default`). The sandbox gets
placeholder `AWS_*` variables, and the OpenShell proxy signs every AWS request
with the real credentials. The provider adds the network rule for the AWS
endpoints. The IAM policy of the profile decides what the agent
can do. Only one profile works per session, so start pibox with the profile
you need:

```sh
AWS_PROFILE=my-profile pibox
```

**Grafana (gcx).** pibox stores each stack token from
`~/.config/gcx/config.yaml` in its own provider. The sandbox gets only the
stack of the current context (`current-context`), through the `GRAFANA_SERVER`
and `GRAFANA_TOKEN` environment variables. The token is a placeholder. The proxy adds the real token to each request from `gcx` to the Grafana
host. The provider adds the network rule for that host. To use another stack, change the current context before you start pibox.

**Kubernetes.** pibox starts one `kubectl proxy` per context of your
kubeconfig, on a random port on `127.0.0.1`. The sandbox gets a kubeconfig
without credentials that points at these proxies. pibox adds the network rule
for the proxy ports after the sandbox starts, so `kubectl` can fail during
the first seconds of a session. `get`, `logs`, `exec`, and
`cp` work. `port-forward` does not, because the OpenShell proxy blocks it.
While the session runs, any local process can use the proxy ports.

The providers stay in the gateway between sessions, and each launch updates
them. List them with `openshell provider list`.

## Docker

`--unsafe-enable-docker` starts a rootless Docker daemon in a privileged
container next to the sandbox, as your user. The agent uses it through
`DOCKER_HOST`, and your working directory is available to its containers at the
same path. The sandbox network policy does not apply to these containers.

## Shell completion

A zsh completion is in `completions/_pibox`. Add the `completions/` directory
to your `$fpath` before `compinit` in `~/.zshrc`:

```zsh
fpath=(/path/to/pibox/completions $fpath)
autoload -U compinit && compinit
```

## What's inside the image

The image has these tools:

- `git`, `vim` (as `$EDITOR`), `tmux`
- `node`, `python` (Python 3), `go`, `gcc`, `make`
- `fd`, `rg`, `jq`, `yq`, `bc`, `zip`, `unzip`, `diffutils`, `curl`
- `docker` and `docker compose`
- `kubectl`, `aws` (aws-cli), `gcx` (Grafana CLI)
- `chromium`, `wl-clipboard`, `patchright` (Playwright), `trafilatura`

The image does not have `sudo`, `ssh`, `scp`, or `wget`.

## Default mounts

| Host                         | Sandbox                      | Mode                       |
| ---------------------------- | ---------------------------- | -------------------------- |
| working directory            | same path                    | rw (ro from `$HOME`)       |
| `~/.pi`                      | `~/.pi`                      | rw                         |
| `~/.pi/agent/extensions`     | `~/.pi/agent/extensions`     | ro                         |
| `~/.claude` (claude)         | `~/.claude`                  | rw                         |
| `~/.claude.json` (claude)    | `~/.claude.json`             | rw                         |
| `~/.gitconfig`               | `~/.gitconfig`               | ro                         |
| `pibox-cache` volume         | `~/.cache`                   | rw                         |

Host mounts that appear under the working directory after launch also appear
in the sandbox.
