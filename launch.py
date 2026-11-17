#!/usr/bin/env python3
"""Run an AI coding agent (pi or Claude Code) in an OpenShell sandbox.

The working directory is mounted into the sandbox at the same path, and the
agent runs as the host user so files keep their ownership.
"""

import argparse
import json
import os
import pwd
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
HOME = Path.home()
HOST_USER = pwd.getpwuid(os.getuid()).pw_name
# The packaged OpenShell gateway runs with PrivateTmp, so bind-mount sources
# must live outside /tmp.
STATE_DIR = HOME / ".cache" / "pibox"

# harness -> (remote image, local image, image it is built from)
IMAGES = {
    "pi": ("ghcr.io/badjware/pibox:pi", "pibox:pi", "pibox:base"),
    "claude": ("ghcr.io/badjware/pibox:claude", "pibox:claude", "pibox:base"),
    "pi-web": ("ghcr.io/badjware/pibox:pi-web", "pibox:pi-web", "pibox:pi"),
}
HARNESS_COMMANDS = {
    "pi": ["pi"],
    "claude": ["claude", "--dangerously-skip-permissions"],
    "pi-web": ["pi-web"],
}
PI_WEB_PORT = "30141"
# Socket directory of the --unsafe-enable-docker sidecar, at the same path in
# the sidecar and the sandbox. It must be outside /run, which rootlesskit
# copies up into its own mount namespace.
DOCKER_RUN_DIR = "/pibox-docker"


def parse_args(argv):
    """Parse the command line.

    Args:
        argv: arguments without the program name. Everything after the first
            "--" is forwarded to the agent.

    Returns:
        A tuple (options namespace, list of agent arguments).
    """
    agent_args = []
    if "--" in argv:
        i = argv.index("--")
        argv, agent_args = argv[:i], argv[i + 1:]

    p = argparse.ArgumentParser(
        prog="pibox",
        description="Run an AI coding agent in an OpenShell sandbox.",
        epilog="Anything after -- is forwarded to the agent inside the sandbox.",
    )
    p.add_argument("-H", "--harness", choices=IMAGES, default="pi", help="agent to run (default: pi)")
    p.add_argument("--build", action="store_true", help="build images locally instead of pulling")
    p.add_argument("--pull", action="store_true", help="pull latest image before launch")
    p.add_argument("--ephemeral", "--tmp", action="store_true", help="use a temp workdir")
    p.add_argument("-w", "--workdir", help="use WORKDIR as the workdir instead of the current directory")
    p.add_argument("-r", "--read-only", "--ro", action="store_true", help="mount the workdir read-only")
    p.add_argument("-v", "--volume", action="append", default=[],
                   help="mount a volume (repeatable; Docker -v syntax); "
                        "PATH or PATH:ro|rw mounts PATH at the same path")
    p.add_argument("-m", "--mount", action="append", default=[],
                   help="add a mount (repeatable; Docker --mount syntax)")
    p.add_argument("-e", "--env", action="append", default=[],
                   help="set an environment variable (repeatable; Docker -e syntax)")
    p.add_argument("-P", "--extra-package", action="append", default=[],
                   help="install an extra pacman or AUR package in the image (repeatable)")
    p.add_argument("-p", "--port", action="append", default=[],
                   help="forward a sandbox port to the same host port (repeatable; [IP:]PORT); "
                        f"pi-web listens on {PI_WEB_PORT} unless PIBOX_PI_WEB_PORT is set")
    p.add_argument("--policy", action="append", default=[],
                   help="merge an OpenShell policy file into the default policy (repeatable)")
    p.add_argument("--unsafe-enable-docker", action="store_true",
                   help="start a rootless Docker daemon in a privileged sidecar container")
    p.add_argument("--unsafe-enable-aws", action="store_true", help="mount ~/.aws into the sandbox")
    p.add_argument("--unsafe-enable-kube", action="store_true", help="mount ~/.kube into the sandbox")
    p.add_argument("--unsafe-enable-gcx", action="store_true", help="mount ~/.config/gcx into the sandbox")
    p.add_argument("--unsafe-host-wayland", action="store_true", help="mount the Wayland socket into the sandbox")
    p.add_argument("--sac-moe-patience", action="store_true", help=argparse.SUPPRESS)
    return p.parse_args(argv), agent_args


def die(msg, code=2):
    """Print an error message and exit with the given code."""
    print(f"pibox: {msg}", file=sys.stderr)
    sys.exit(code)


def confirm(msg, skip):
    """Ask the user to confirm a risky option; exit unless they answer yes.

    Args:
        msg: what the option does.
        skip: when true, only print the warning.
    """
    if skip:
        print(f"pibox: warning: {msg} (skipped by --sac-moe-patience)", file=sys.stderr)
        return
    print(f"pibox: warning: {msg}", file=sys.stderr)
    reply = input("proceed? [y/N] ")
    if reply.strip().lower() != "y":
        sys.exit(1)


def run(cmd, **kwargs):
    """Run a command and raise CalledProcessError if it fails."""
    return subprocess.run(cmd, check=True, **kwargs)


def deep_merge(base, override):
    """Return base deep-merged with override; values from override win."""
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    merged = dict(base)
    for key, value in override.items():
        merged[key] = deep_merge(base[key], value) if key in base else value
    return merged


def merge_policies(policies):
    """Merge OpenShell policies in order.

    network_policies rules are replaced by name, filesystem_policy path lists
    are concatenated without duplicates, and every other key is deep-merged
    with later policies winning.

    Args:
        policies: list of policy dicts.

    Returns:
        The merged policy dict.
    """
    merged = {}
    for policy in policies:
        rules = {name: rule for name, rule in merged.get("network_policies", {}).items()
                 if name not in policy.get("network_policies", {})}
        old_fs = merged.get("filesystem_policy", {})
        new_fs = policy.get("filesystem_policy", {})
        merged = deep_merge({**merged, "network_policies": rules}, policy)
        for key in ("read_only", "read_write"):
            paths = old_fs.get(key, []) + new_fs.get(key, [])
            merged.setdefault("filesystem_policy", {})[key] = list(dict.fromkeys(paths))
    return merged


def load_user_policy(path):
    """Load a --policy file.

    The process section is rejected because the workload identity must match
    the per-user image.

    Returns:
        The policy dict.
    """
    try:
        policy = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as e:
        die(f"--policy {path}: {e}")
    if not isinstance(policy, dict):
        die(f"--policy {path}: not a YAML mapping")
    if "process" in policy:
        die(f"--policy {path}: the process section is set by pibox")
    return policy


def mount(kind, source, read_only=False, rslave=False, size_bytes=None):
    """Return a mount entry for the mount table.

    Args:
        kind: "bind", "volume", or "tmpfs".
        source: host path for a bind mount, volume name for a volume, None for tmpfs.
        read_only: mount read-only.
        rslave: propagate host mounts made under a bind source after launch.
        size_bytes: size limit of a tmpfs mount.
    """
    return {"type": kind, "source": source, "read_only": read_only, "rslave": rslave, "size_bytes": size_bytes}


def parse_volume(spec):
    """Parse a --volume value (Docker -v syntax).

    PATH or PATH:ro|rw is shorthand for a bind mount at the same path.

    Returns:
        A tuple (target path, mount entry).
    """
    if ":" not in spec or re.fullmatch(r"[^:]+:(ro|rw)", spec):
        path, _, opts = spec.partition(":")
        path = str(Path(path).resolve(strict=True))
        spec = f"{path}:{path}" + (f":{opts}" if opts else "")
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        die(f"invalid --volume: {spec}")
    source, target = parts[0], parts[1]
    read_only = rslave = False
    for opt in parts[2].split(",") if len(parts) == 3 else []:
        if opt in ("ro", "readonly"):
            read_only = True
        elif opt == "rw":
            read_only = False
        elif opt in ("rslave", "slave"):
            rslave = True
        elif opt not in ("private", "rprivate"):
            die(f"unsupported --volume option: {opt}")
    return target, mount("bind" if source.startswith("/") else "volume", source, read_only, rslave)


def parse_mount(spec):
    """Parse a --mount value (Docker --mount syntax).

    Supports type, source/src, target/destination/dst, readonly/ro,
    bind-propagation, and tmpfs-size.

    Returns:
        A tuple (target path, mount entry).
    """
    fields = {}
    for item in spec.split(","):
        key, eq, value = item.partition("=")
        fields[key] = value if eq else "true"
    kind = fields.pop("type", "volume")
    source = fields.pop("source", None) or fields.pop("src", None)
    target = fields.pop("target", None) or fields.pop("destination", None) or fields.pop("dst", None)
    readonly = fields.pop("readonly", None) or fields.pop("ro", None) or "false"
    propagation = fields.pop("bind-propagation", "rprivate")
    size = fields.pop("tmpfs-size", None)
    if fields:
        die(f"unsupported --mount keys: {', '.join(fields)}")
    if kind not in ("bind", "volume", "tmpfs") or not target or (kind != "tmpfs" and not source):
        die(f"invalid --mount: {spec}")
    if propagation not in ("rslave", "slave", "private", "rprivate"):
        die(f"unsupported --mount bind-propagation: {propagation}")
    return target, mount(kind, source, readonly in ("true", "1"), propagation in ("rslave", "slave"),
                         int(size) if size else None)


def parse_env(spec):
    """Parse an --env value (Docker -e syntax).

    Returns:
        A tuple (name, value). VAR alone copies the host value; the value is
        None when VAR is unset on the host.
    """
    name, eq, value = spec.partition("=")
    return name, value if eq else os.environ.get(name)


def parse_port(spec):
    """Parse a --port value into an openshell forward spec ([IP:]PORT).

    Docker -p syntax is accepted when the host and sandbox ports are equal,
    because openshell forwards a port to the same port number.
    """
    spec, _, proto = spec.partition("/")
    if proto not in ("", "tcp"):
        die(f"--port {spec}/{proto}: only tcp is supported")
    if spec.startswith("["):
        ip, _, rest = spec.partition("]:")
        ip, ports = ip + "]", rest.split(":")
    else:
        parts = spec.split(":")
        ip = parts[0] if len(parts) == 3 or (len(parts) == 2 and not parts[0].isdigit()) else ""
        ports = parts[1:] if ip else parts
    if not 1 <= len(ports) <= 2 or not all(p.isdigit() for p in ports) or ports[0] != ports[-1]:
        die(f"--port {spec}: openshell forwards a sandbox port to the same host port ([IP:]PORT)")
    return f"{ip}:{ports[-1]}" if ip else ports[-1]


def host_timezone():
    """Return the host time zone name, or None if it cannot be found."""
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return None


def build_images(harness, build, pull):
    """Make sure the harness image is available.

    Args:
        harness: harness name, a key of IMAGES.
        build: build the images locally instead of using the remote image.
        pull: pull the remote image before launch.

    Returns:
        The name of the harness image.
    """
    remote, local, base = IMAGES[harness]
    if not build:
        if pull:
            run(["docker", "pull", remote])
        return remote

    secret = []
    if (HOME / ".npmrc").is_file():
        secret = ["--secret", f"id=npmrc,src={HOME / '.npmrc'}"]
    chain = [("pibox:base", "base", None)]
    if base == "pibox:pi":
        chain.append(("pibox:pi", "pi", "pibox:base"))
    chain.append((local, harness, base))
    for tag, name, parent in chain:
        cmd = ["docker", "build", "-t", tag, "-f", str(SCRIPT_DIR / f"Dockerfile.{name}"), *secret]
        cmd += ["--pull"] if parent is None else ["--build-arg", f"BASE_IMAGE={parent}"]
        run([*cmd, str(SCRIPT_DIR)])
    return local


def build_user_image(harness, image, extra_packages):
    """Build the per-user layer on top of the harness image.

    OpenShell runs the workload as a non-root user and ignores the image
    entrypoint, so the host user and extra packages are baked into the image.

    Returns:
        The name of the per-user image.
    """
    tag = f"pibox-user:{harness}"
    print(f"pibox: building {tag}", file=sys.stderr)
    run([
        "docker", "build", "-q", "-t", tag, "-f", str(SCRIPT_DIR / "Dockerfile.user"),
        "--build-arg", f"BASE_IMAGE={image}",
        "--build-arg", f"HOST_UID={os.getuid()}",
        "--build-arg", f"HOST_GID={os.getgid()}",
        "--build-arg", f"HOST_USER={HOST_USER}",
        "--build-arg", f"HOST_HOME={HOME}",
        "--build-arg", f"EXTRA_PACKAGES={' '.join(extra_packages)}",
        str(SCRIPT_DIR / "common"),
    ], stdout=subprocess.DEVNULL)
    return tag


def remove_volume(name):
    """Remove a Docker volume, retrying while the sandbox container shuts down."""
    for _ in range(20):
        if subprocess.run(["docker", "volume", "rm", name], capture_output=True).returncode == 0:
            return
        time.sleep(0.5)
    print(f"pibox: warning: could not remove volume {name}", file=sys.stderr)


def start_docker_sidecar(name, image, workdir, run_dir):
    """Start a rootless dockerd in a privileged container next to the sandbox.

    OpenShell sandboxes cannot run a Docker daemon, so the agent reaches this
    one through its socket. The workdir is mounted at the same path so that
    bind mounts from the agent resolve to the same files.

    Args:
        name: container name.
        image: per-user image, so dockerd runs as the host user.
        workdir: workdir path.
        run_dir: host directory that receives the socket.
    """
    run([
        "docker", "run", "-d", "--rm", "--privileged", "--name", name,
        "-v", f"{workdir}:{workdir}:rslave",
        "-v", f"{run_dir}:{DOCKER_RUN_DIR}",
        "-e", f"HOME={HOME}",
        "-e", f"XDG_RUNTIME_DIR={DOCKER_RUN_DIR}",
        "-e", f"XDG_DATA_HOME={HOME}/.local/share",
        "--entrypoint", "dockerd-rootless.sh",
        image,
        f"--host=unix://{DOCKER_RUN_DIR}/docker.sock", "--storage-driver=fuse-overlayfs",
    ], stdout=subprocess.DEVNULL)
    host_socket = f"unix://{run_dir}/docker.sock"
    for _ in range(60):
        if subprocess.run(["docker", "-H", host_socket, "version"], capture_output=True).returncode == 0:
            return
        if subprocess.run(["docker", "container", "inspect", name], capture_output=True).returncode != 0:
            break
        time.sleep(0.5)
    subprocess.run(["docker", "logs", "--tail", "50", name])
    die("the rootless Docker daemon did not start", 1)


def keep_forwards(sandbox_name, ports, stop):
    """Keep openshell port forwards running until stop is set.

    A forward can only start once the sandbox is ready, and it exits when the
    service in the sandbox is not listening yet, so it is restarted as needed.

    Args:
        sandbox_name: name of the sandbox.
        ports: forward specs ([IP:]PORT).
        stop: threading.Event that ends the loop.
    """
    while not stop.wait(1):
        out = subprocess.run(["openshell", "forward", "list", "-o", "json"], capture_output=True, text=True).stdout
        try:
            alive = {f["port"] for f in json.loads(out or "[]") if f["sandbox"] == sandbox_name and f["alive"]}
        except (ValueError, KeyError, TypeError):
            alive = set()
        for spec in ports:
            port = int(spec.rsplit(":", 1)[-1])
            if port not in alive:
                subprocess.run(["openshell", "forward", "stop", str(port), sandbox_name], capture_output=True)
                subprocess.run(["openshell", "forward", "start", "-d", spec, sandbox_name], capture_output=True)


def main():
    """Entry point. Returns the exit status of the agent."""
    opts, agent_args = parse_args(sys.argv[1:])

    # parse everything before prompting or starting anything
    user_mounts = [parse_volume(v) for v in opts.volume] + [parse_mount(m) for m in opts.mount]
    user_env = dict(parse_env(e) for e in opts.env)
    user_policies = [load_user_policy(p) for p in opts.policy]
    ports = [parse_port(p) for p in opts.port]

    skip = opts.sac_moe_patience
    if os.getuid() == 0:
        confirm("running as root", skip)
    if opts.unsafe_enable_docker:
        confirm("--unsafe-enable-docker starts a privileged container, "
                "and the network policy does not apply to the containers it runs", skip)
    if opts.unsafe_enable_aws:
        confirm("--unsafe-enable-aws mounts ~/.aws into the sandbox", skip)
    if opts.unsafe_enable_kube:
        confirm("--unsafe-enable-kube mounts ~/.kube into the sandbox", skip)
    if opts.unsafe_enable_gcx:
        confirm("--unsafe-enable-gcx mounts ~/.config/gcx into the sandbox", skip)
    if opts.unsafe_host_wayland:
        confirm("--unsafe-host-wayland mounts the Wayland socket into the sandbox", skip)
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        wayland_socket = Path(runtime_dir) / os.environ.get("WAYLAND_DISPLAY", "")
        if not os.environ.get("WAYLAND_DISPLAY"):
            die("--unsafe-host-wayland requires WAYLAND_DISPLAY to be set")
        if not wayland_socket.is_socket():
            die(f"Wayland socket not found: {wayland_socket}")
    for port in ports:
        confirm(f"--port publishes {port} on the host", skip)

    if opts.workdir and opts.ephemeral:
        die("--workdir cannot be used with --ephemeral")
    workdir = Path(opts.workdir or os.getcwd()).resolve(strict=True)

    status = subprocess.run(["openshell", "status", "-o", "json"], capture_output=True, text=True)
    if status.returncode != 0 or json.loads(status.stdout or "{}").get("status") != "connected":
        die("the OpenShell gateway is not reachable (see contrib/openshell/gateway.toml)", 1)

    # ensure these exist on the host so Docker does not create them as root
    (HOME / ".pi" / "agent" / "extensions").mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if opts.harness == "claude":
        (HOME / ".claude").mkdir(exist_ok=True)
        (HOME / ".claude.json").touch()
    unsafe_dirs = [(opts.unsafe_enable_aws, HOME / ".aws"), (opts.unsafe_enable_kube, HOME / ".kube"),
                   (opts.unsafe_enable_gcx, HOME / ".config" / "gcx")]
    for enabled, path in unsafe_dirs:
        if enabled:
            path.mkdir(parents=True, exist_ok=True)

    sandbox_name = f"pibox-{os.getpid()}"  # openshell limits names to 19 characters
    rslave_volumes = []
    docker_sidecar = None
    stop_forwards = threading.Event()
    tmp = tempfile.TemporaryDirectory(dir=STATE_DIR, prefix="tmp.")
    try:
        if opts.ephemeral:
            workdir = Path(tmp.name) / "workdir"
            workdir.mkdir()
            # save pi sessions in the temp workdir
            if opts.harness == "pi" and "--session-dir" not in agent_args:
                session_dir = workdir / ".pi" / "sessions"
                session_dir.mkdir(parents=True)
                agent_args += ["--session-dir", str(session_dir)]

        image = build_images(opts.harness, opts.build, opts.pull)
        user_image = build_user_image(opts.harness, image, opts.extra_package)

        # Resolve the final mount list.
        # Priority: defaults < user-provided < workdir. A later entry for the
        # same target wins, but keeps the position of the first one.
        mounts = {
            HOME / ".pi": mount("bind", HOME / ".pi"),
            HOME / ".pi/agent/extensions": mount("bind", HOME / ".pi/agent/extensions", read_only=True),
            HOME / ".cache": mount("volume", "pibox-cache"),
        }
        if opts.harness == "claude":
            mounts[HOME / ".claude"] = mount("bind", HOME / ".claude")
            # claude really hates to have its config file read-only
            mounts[HOME / ".claude.json"] = mount("bind", HOME / ".claude.json")
        if (HOME / ".gitconfig").is_file():
            mounts[HOME / ".gitconfig"] = mount("bind", HOME / ".gitconfig", read_only=True)
        for path in map(Path, ["/etc/fonts", "/usr/share/fonts", "/var/cache/fontconfig"]):
            if path.is_dir():
                mounts[path] = mount("bind", path, read_only=True)
        for enabled, path in unsafe_dirs:
            if enabled:
                mounts[path] = mount("bind", path, read_only=True)
        if opts.unsafe_host_wayland:
            mounts[wayland_socket] = mount("bind", wayland_socket)
        if opts.unsafe_enable_docker:
            docker_run_dir = Path(tmp.name) / "docker"
            docker_run_dir.mkdir()
            mounts[Path(DOCKER_RUN_DIR)] = mount("bind", docker_run_dir)
        for target, entry in user_mounts:
            mounts[Path(target)] = entry
        # When launched from $HOME, the workdir is read-only to avoid exposing
        # the whole home directory rw. An explicit --volume for $HOME wins.
        if not (workdir == HOME and HOME in mounts and not opts.read_only):
            mounts[workdir] = mount("bind", workdir, read_only=opts.read_only or workdir == HOME, rslave=True)

        driver_mounts = []
        fs_policy = {"read_only": [], "read_write": [str(HOME)]}
        for target, entry in mounts.items():
            source = str(entry["source"])
            if entry["type"] == "bind":
                if source == "/tmp" or source.startswith("/tmp/"):
                    die(f"cannot mount {source}: the OpenShell gateway runs with a private /tmp")
                if not Path(source).exists():
                    die(f"cannot mount {source}: no such file or directory")
            if entry["type"] == "bind" and entry["rslave"]:
                # OpenShell bind mounts are always rprivate; a local volume
                # with rslave lets host mounts made under the source after
                # launch appear in the sandbox.
                name = f"{sandbox_name}-{len(rslave_volumes)}"
                run(["docker", "volume", "create", "--driver", "local", "--opt", "type=none",
                     "--opt", f"device={source}", "--opt", "o=rbind,rslave", name], stdout=subprocess.DEVNULL)
                rslave_volumes.append(name)
                driver_mounts.append({"type": "volume", "source": name, "target": str(target),
                                      "read_only": entry["read_only"]})
            elif entry["type"] == "tmpfs":
                driver_mounts.append({"type": "tmpfs", "target": str(target),
                                      **({"size_bytes": entry["size_bytes"]} if entry["size_bytes"] else {})})
            else:
                if entry["type"] == "volume":
                    # docker run creates missing named volumes; the openshell driver does not
                    run(["docker", "volume", "create", source], stdout=subprocess.DEVNULL)
                driver_mounts.append({"type": entry["type"], "source": source, "target": str(target),
                                      "read_only": entry["read_only"]})
            fs_policy["read_only" if entry["read_only"] else "read_write"].append(str(target))

        policy = merge_policies([
            yaml.safe_load((SCRIPT_DIR / "common" / "policy.yaml").read_text()),
            *user_policies,
            {"filesystem_policy": fs_policy,
             "process": {"run_as_user": str(os.getuid()), "run_as_group": str(os.getgid())}},
        ])
        policy_file = Path(tmp.name) / "policy.yaml"
        policy_file.write_text(yaml.safe_dump(policy, sort_keys=False))

        env = {
            "HOME": str(HOME),
            "USER": HOST_USER,
            "TERM": os.environ.get("TERM") or "xterm-256color",
            "TZ": host_timezone(),
            "COLORTERM": os.environ.get("COLORTERM"),
        }
        if opts.harness == "claude":
            # no auto-updater or telemetry; the network policy blocks them anyway
            env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        if opts.unsafe_host_wayland:
            env["XDG_RUNTIME_DIR"] = runtime_dir
            env["WAYLAND_DISPLAY"] = os.environ["WAYLAND_DISPLAY"]
            env["XDG_SESSION_TYPE"] = "wayland"
        if opts.unsafe_enable_docker:
            env["DOCKER_HOST"] = f"unix://{DOCKER_RUN_DIR}/docker.sock"
        if opts.harness == "pi-web":
            # openshell forwards to the same port number, so listen on the host port
            env["PI_WEB_HOSTNAME"] = "127.0.0.1"
            env["PORT"] = PI_WEB_PORT
            if not ports:
                env["PORT"] = os.environ.get("PIBOX_PI_WEB_PORT") or PI_WEB_PORT
                ports = [f"127.0.0.1:{env['PORT']}"]
        env.update(user_env)

        tty = sys.stdin.isatty() and sys.stdout.isatty()
        # Without a TTY, openshell never forwards EOF on stdin, so an agent
        # that reads stdin (pi -p) would wait forever.
        script = 'cd -- "$1" && shift && exec "$@"' + ("" if tty else " </dev/null")
        cmd = [
            "openshell", "sandbox", "create",
            "--name", sandbox_name,
            "--from", user_image,
            "--policy", str(policy_file),
            "--driver-config-json", json.dumps({"docker": {"mounts": driver_mounts}}),
            "--no-keep",
            "--no-auto-providers",
            "--tty" if tty else "--no-tty",
            *[arg for key, value in env.items() if value is not None for arg in ("--env", f"{key}={value}")],
            "--", "bash", "-c", script, "pibox", str(workdir), *HARNESS_COMMANDS[opts.harness], *agent_args,
        ]
        if opts.unsafe_enable_docker:
            docker_sidecar = f"{sandbox_name}-docker"
            start_docker_sidecar(docker_sidecar, user_image, workdir, docker_run_dir)
        if ports:
            # --forward cannot be combined with --no-keep
            threading.Thread(target=keep_forwards, args=(sandbox_name, ports, stop_forwards), daemon=True).start()
        print(f"+ {shlex.join(cmd)}", file=sys.stderr)
        return subprocess.run(cmd).returncode
    finally:
        stop_forwards.set()
        for spec in ports:
            subprocess.run(["openshell", "forward", "stop", spec.rsplit(":", 1)[-1], sandbox_name],
                           capture_output=True)
        # --no-keep cannot clean up when the openshell CLI itself is killed
        subprocess.run(["openshell", "sandbox", "delete", sandbox_name], capture_output=True)
        if docker_sidecar:
            subprocess.run(["docker", "rm", "-f", docker_sidecar], capture_output=True)
        for name in rslave_volumes:
            remove_volume(name)
        tmp.cleanup()


def exit_on_signal(signum, _frame):
    """Turn a termination signal into SystemExit so cleanup still runs."""
    sys.exit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, exit_on_signal)
    signal.signal(signal.SIGHUP, exit_on_signal)
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        die(f"command failed: {shlex.join(map(str, e.cmd))}", e.returncode)
    except KeyboardInterrupt:
        sys.exit(130)
