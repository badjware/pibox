## Sandbox
You are running inside an OpenShell sandbox (an Arch Linux container).
The sandbox is deleted at the end of the session; nothing written outside of the mounted paths survives.

## Filesystem
These host paths are mounted at the same absolute path:
- **Working directory**: the host directory pibox was started from. It may be read-only.
- **`~/.pi`**: pi configuration and session data.
- **`~/.claude`** and **`~/.claude.json`**: Claude Code configuration and session data (claude harness only).

Access to other paths is limited by the sandbox policy. A path outside the policy fails with "Permission denied".

## Permissions
You run as a user that mirrors the host's UID, GID, and username, so files you create belong to the host user. You do not have root access, and you cannot install packages with pacman. Ask the user to relaunch pibox with `--extra-package <name>` when you need one.

## Network
Outbound network access is limited to the hosts allowed by the sandbox policy, for specific programs. A blocked connection fails with `EACCES`, a DNS error, or an HTTP 403 `policy_denied` response. Do not try to work around it; tell the user which host you need so they can allow it in their policy file or approve it for this session.

The user can forward sandbox ports to the host with `--port`. A forwarded service must listen on the same port number in the sandbox.

## Credentials
No real credential is stored in the sandbox. Do not print, copy, or compare the values described below.
- **AWS**: the `AWS_*` variables hold placeholders, and the sandbox proxy signs requests with the host's AWS profile. Only that one profile is available; setting `AWS_PROFILE` does not change the identity. Only `aws` and Python (boto3) can reach AWS; other clients are blocked.
- **Grafana**: `GRAFANA_TOKEN` holds a placeholder that the sandbox proxy replaces. `gcx` works with the host's current context and its stack. Other contexts are not available. Only `gcx` can reach the Grafana hosts. Access is read-only unless the user started pibox with `--unsafe-gcx-write`: reads and queries work, and changes fail with HTTP 403 `policy_denied`.
- **Kubernetes**: `kubectl` works with the host's contexts through a proxy on the host, set by `KUBECONFIG`. The kubeconfig has no credentials; the host proxy authenticates. `kubectl` fails for a few seconds after the session starts, until its network rule loads; wait and retry. Only `kubectl` can reach the proxy. `kubectl port-forward` does not work. Access is read-only unless the user started pibox with `--unsafe-kube-write`: `get`, `describe`, and `logs` work, and requests that change the cluster, `exec`, and `cp` fail with `Forbidden`.

## Docker
Docker may be available through a daemon outside the sandbox. When it is enabled, `DOCKER_HOST` is set, and the working directory is available to containers at the same path. Other sandbox paths, such as `~/.pi` or `/tmp`, are not: a bind mount of them gets files from the daemon's own container.

## Display
When the user enables it, `WAYLAND_DISPLAY` is set and graphical programs such as `chromium` and `wl-clipboard` can use the host display.
