#!/usr/bin/env python3
"""Run an AI coding agent (pi or Claude Code) in a sandboxed Docker container.

The working directory is bind-mounted into the container at the same path,
and the entrypoint runs the agent as the host user so files keep their
ownership.
"""

import argparse
import fcntl
import os
import pwd
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
HOME = Path.home()
HOST_UID = os.environ.get("HOST_UID") or str(os.getuid())
HOST_GID = os.environ.get("HOST_GID") or str(os.getgid())
HOST_USER = os.environ.get("HOST_USER") or pwd.getpwuid(os.getuid()).pw_name

# harness -> (remote image, local image, image it is built from)
IMAGES = {
    "pi": ("ghcr.io/badjware/pibox:pi", "pibox:pi", "pibox:base"),
    "claude": ("ghcr.io/badjware/pibox:claude", "pibox:claude", "pibox:base"),
    "pi-web": ("ghcr.io/badjware/pibox:pi-web", "pibox:pi-web", "pibox:pi"),
}
PI_WEB_PORT = "30141"
# Probed in order when SSL_CERT_FILE is not set on the host.
CA_BUNDLES = [
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
]
# Image of the --unsafe-enable-docker sidecar.
DIND_IMAGE = "docker:dind-rootless"
# Socket directory of the --unsafe-enable-docker sidecar, at the same path in
# both containers. It must be outside /run, which rootlesskit copies up into
# its own mount namespace.
DOCKER_RUN_DIR = "/pibox-docker"
# Data directory of the sidecar daemon, a named volume that persists the
# images and the build cache between sessions.
DOCKER_DATA_DIR = "/pibox-docker-data"
# Run as root in the sidecar. DIND_IMAGE only has passwd and subuid entries
# for its own user (UID 1000), and the daemon must run as the host user so
# that files written in the workdir belong to the host user.
DIND_SCRIPT = r'''set -e
grep -q "^[^:]*:[^:]*:$HOST_GID:" /etc/group || addgroup -g "$HOST_GID" "$HOST_USER"
group=$(grep "^[^:]*:[^:]*:$HOST_GID:" /etc/group | cut -d: -f1)
grep -q "^[^:]*:[^:]*:$HOST_UID:" /etc/passwd || adduser -D -u "$HOST_UID" -G "$group" -h "/home/$HOST_USER" "$HOST_USER"
user=$(grep "^[^:]*:[^:]*:$HOST_UID:" /etc/passwd | cut -d: -f1)
home=$(grep "^[^:]*:[^:]*:$HOST_UID:" /etc/passwd | cut -d: -f6)
grep -q "^$user:" /etc/subuid || echo "$user:100000:65536" >> /etc/subuid
grep -q "^$user:" /etc/subgid || echo "$user:100000:65536" >> /etc/subgid
# the image entrypoint checks that ~/.local/share/docker is writable
mkdir -p "$home/.local/share/docker"
chown "$HOST_UID:$HOST_GID" "$home" "$home/.local" "$home/.local/share" "$home/.local/share/docker" \
    "$XDG_RUNTIME_DIR" "$DOCKER_DATA_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
# without arguments, the image entrypoint also listens on TCP
exec su -s /bin/sh "$user" -c \
    "exec dockerd-entrypoint.sh dockerd --host=unix://$XDG_RUNTIME_DIR/docker.sock --data-root=$DOCKER_DATA_DIR"
'''


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
        description="Run an AI coding agent in a sandboxed Docker container.",
        epilog="Anything after -- is forwarded to the agent inside the container.",
    )
    p.add_argument("-H", "--harness", choices=IMAGES, default="pi", help="agent to run (default: pi)")
    p.add_argument("--build", action="store_true", help="build images locally instead of pulling")
    p.add_argument("--pull", action="store_true", help="pull latest image before launch")
    p.add_argument("-r", "--read-only", "--ro", action="store_true", help="mount the workdir read-only")
    p.add_argument("-v", "--volume", action="append", default=[],
                   help="bind-mount an extra volume (repeatable; Docker -v syntax); "
                        "PATH or PATH:ro|rw mounts PATH at the same path")
    p.add_argument("-e", "--env", action="append", default=[],
                   help="set an environment variable (repeatable; Docker -e syntax)")
    p.add_argument("-P", "--extra-package", action="append", default=[],
                   help="install an extra pacman or AUR package at startup (repeatable)")
    p.add_argument("-p", "--port", action="append", default=[],
                   help="publish a container port (repeatable; Docker -p syntax); "
                        f"pi-web listens on {PI_WEB_PORT}; default 127.0.0.1:{PI_WEB_PORT}:{PI_WEB_PORT}")
    p.add_argument("--unsafe-enable-docker", action="store_true",
                   help="start a rootless Docker daemon in a privileged sidecar container")
    p.add_argument("--unsafe-enable-aws", action="store_true", help="mount ~/.aws into the container")
    p.add_argument("--unsafe-enable-kube", action="store_true", help="mount ~/.kube into the container")
    p.add_argument("--unsafe-enable-gcx", action="store_true", help="mount ~/.config/gcx into the container")
    p.add_argument("--unsafe-host-wayland", action="store_true", help="mount the Wayland socket into the container")
    p.add_argument("--unsafe-host-net", action="store_true", help="share the host network namespace")
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


def run(cmd):
    """Run a command and exit with its status if it fails."""
    result = subprocess.run(cmd)
    if result.returncode != 0:
        die(f"command failed: {shlex.join(cmd)}", result.returncode)


def parse_volume(spec):
    """Parse a --volume value (Docker -v syntax).

    PATH or PATH:ro|rw is shorthand for a bind mount at the same path.

    Returns:
        A tuple (container destination, Docker -v spec).
    """
    if ":" not in spec or re.fullmatch(r"[^:]+:(ro|rw)", spec):
        path, _, opts = spec.partition(":")
        try:
            path = str(Path(path).resolve(strict=True))
        except FileNotFoundError:
            die(f"--volume {spec}: no such file or directory")
        spec = f"{path}:{path}" + (f":{opts}" if opts else "")
    parts = spec.split(":")
    if len(parts) not in (2, 3):
        die(f"invalid --volume: {spec}")
    return os.path.normpath(parts[1]), spec


def host_timezone():
    """Return the host time zone name, or an empty string if it cannot be found."""
    if os.environ.get("TZ"):
        return os.environ["TZ"]
    try:
        return os.readlink("/etc/localtime").split("zoneinfo/", 1)[1]
    except (OSError, IndexError):
        return ""


def host_ca_bundle():
    """Return the path of the host CA bundle, or None if there is none."""
    for path in [os.environ.get("SSL_CERT_FILE"), *CA_BUNDLES]:
        if path and os.path.isfile(path):
            return path
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


def pick_container_name(workdir, suffix=""):
    """Return a free container name based on the working directory name.

    Like the tmux sessions: "home" for $HOME, else the directory name, with
    -2, -3, ... appended when the name is taken.

    Args:
        workdir: resolved working directory.
        suffix: appended to the directory name, before the number.
    """
    base = "home" if workdir == HOME else re.sub(r"[^a-z0-9-]", "-", workdir.name.lower()).strip("-") or "root"
    base += suffix
    out = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True, text=True).stdout
    taken = set(out.split())
    name, i = base, 2
    while name in taken:
        name, i = f"{base}-{i}", i + 1
    return name


def docker_sidecar(workdir, workdir_volume, ca_bundle):
    """Return the Docker sidecar of the workdir, and start it if needed.

    Sessions with the same workdir mount share one sidecar: a rootless dockerd
    in a privileged container next to the agents. The agents reach the daemon
    through its socket in the volume of the same name, mounted at
    DOCKER_RUN_DIR. The workdir is mounted at the same path so that bind
    mounts from the agents resolve to the same files. The daemon data is in
    a named volume per workdir mount, which is kept after the sidecar stops;
    only one sidecar at a time uses it, because there is at most one sidecar
    per workdir mount.

    Call with the session lock held.

    Args:
        workdir: resolved working directory.
        workdir_volume: Docker -v spec of the workdir in the agent container.
        ca_bundle: host CA bundle path, or None.

    Returns:
        The name of the sidecar container and of its socket volume.
    """
    label = f"pibox.docker-workdir={workdir_volume}"
    out = subprocess.run(["docker", "ps", "--filter", f"label={label}", "--format", "{{.Names}}"],
                         capture_output=True, text=True).stdout.split()
    if out:
        wait_for_daemon(out[0])
        return out[0]

    name = pick_container_name(workdir, "-docker")
    read_only = "ro" in (workdir_volume.split(":") + [""])[2].split(",")
    data_volume = "pibox-docker-" + re.sub(r"[^a-z0-9-]", "-", str(workdir).lower()).strip("-") + \
        ("-ro" if read_only else "")
    # a volume left by a killed session would keep a stale socket
    subprocess.run(["docker", "volume", "rm", name], capture_output=True)
    cmd = [
        "docker", "run", "-d", "--rm", "--privileged", "--user", "0", "--name", name, "--label", label,
        "-v", workdir_volume,
        "-v", f"{name}:{DOCKER_RUN_DIR}",
        "-v", f"{data_volume}:{DOCKER_DATA_DIR}",
        "-e", f"HOST_UID={HOST_UID}",
        "-e", f"HOST_GID={HOST_GID}",
        "-e", f"HOST_USER={HOST_USER}",
        "-e", f"XDG_RUNTIME_DIR={DOCKER_RUN_DIR}",
        "-e", f"DOCKER_DATA_DIR={DOCKER_DATA_DIR}",
    ]
    if ca_bundle:
        cmd += ["-v", f"{ca_bundle}:/etc/ssl/host-ca-bundle.pem:ro",
                "-e", "SSL_CERT_FILE=/etc/ssl/host-ca-bundle.pem"]
    cmd += [DIND_IMAGE, "sh", "-c", DIND_SCRIPT]
    print(f"+ {shlex.join(cmd[:-1])} <DIND_SCRIPT>", file=sys.stderr)
    if subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode != 0:
        die("could not start the Docker sidecar", 1)
    wait_for_daemon(name)
    return name


def wait_for_daemon(name):
    """Wait until the Docker daemon of the sidecar NAME answers; exit if it does not."""
    for _ in range(60):
        version = ["docker", "exec", name, "docker", "-H", f"unix://{DOCKER_RUN_DIR}/docker.sock", "version"]
        if subprocess.run(version, capture_output=True).returncode == 0:
            return
        if subprocess.run(["docker", "container", "inspect", name], capture_output=True).returncode != 0:
            break
        time.sleep(0.5)
    subprocess.run(["docker", "logs", "--tail", "50", name])
    die("the rootless Docker daemon did not start", 1)


def lock_sessions():
    """Take an exclusive lock shared by every pibox session on this host.

    It serializes the start and the removal of the shared Docker sidecars.
    The lock is on this script, so no file is created.

    Returns:
        The locked file; closing it releases the lock.
    """
    f = open(__file__)
    fcntl.flock(f, fcntl.LOCK_EX)
    return f


def remove_volume(name):
    """Remove a Docker volume, retrying while its containers shut down."""
    for _ in range(20):
        if subprocess.run(["docker", "volume", "rm", name], capture_output=True).returncode == 0:
            return
        time.sleep(0.5)
    print(f"pibox: warning: could not remove volume {name}", file=sys.stderr)


def main():
    """Entry point. Returns the exit status of the agent."""
    opts, agent_args = parse_args(sys.argv[1:])
    user_volumes = [parse_volume(v) for v in opts.volume]

    skip = opts.sac_moe_patience
    if opts.unsafe_enable_docker:
        confirm("--unsafe-enable-docker starts a privileged container, "
                "and the containers it runs are not restricted like the agent container", skip)
    if opts.unsafe_enable_aws:
        confirm("--unsafe-enable-aws mounts ~/.aws into the container", skip)
    if opts.unsafe_enable_kube:
        confirm("--unsafe-enable-kube mounts ~/.kube into the container", skip)
    if opts.unsafe_enable_gcx:
        confirm("--unsafe-enable-gcx mounts ~/.config/gcx into the container", skip)
    if opts.unsafe_host_wayland:
        confirm("--unsafe-host-wayland mounts the Wayland socket into the container", skip)
        if not os.environ.get("WAYLAND_DISPLAY"):
            die("--unsafe-host-wayland requires WAYLAND_DISPLAY to be set")
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{HOST_UID}"
        wayland_socket = Path(runtime_dir) / os.environ["WAYLAND_DISPLAY"]
        if not wayland_socket.is_socket():
            die(f"Wayland socket not found: {wayland_socket}")
    if opts.unsafe_host_net:
        confirm("--unsafe-host-net shares the host network namespace", skip)
        if opts.port:
            die("--port cannot be used with --unsafe-host-net")

    workdir = Path.cwd().resolve()
    image = build_images(opts.harness, opts.build, opts.pull)

    name = pick_container_name(workdir)

    # Drop all caps and re-add only what the entrypoint needs.
    # Also block setuid/setgid binaries from gaining new privileges.
    args = ["--security-opt=no-new-privileges:true", "--security-opt=seccomp=unconfined", "--cap-drop=ALL"]
    args += [f"--cap-add={cap}" for cap in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID", "KILL")]

    if opts.unsafe_host_net:
        args += ["--network=host"]
    for port in opts.port:
        args += ["-p", port]

    env = {
        "TZ": host_timezone(),
        "COLORTERM": os.environ.get("COLORTERM", ""),
        "TERM": os.environ.get("TERM", ""),
        "HOST_UID": HOST_UID,
        "HOST_GID": HOST_GID,
        "HOST_USER": HOST_USER,
        "HARNESS": opts.harness,
        "EXTRA_PACKAGES": " ".join(opts.extra_package),
    }
    if opts.harness == "claude":
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    if opts.harness == "pi-web":
        if opts.unsafe_host_net:
            # the host network namespace exposes 0.0.0.0 on every host interface
            env["PI_WEB_HOSTNAME"] = "127.0.0.1"
            env["PORT"] = os.environ.get("PIBOX_PI_WEB_PORT") or PI_WEB_PORT
        elif not opts.port:
            args += ["-p", f"127.0.0.1:{os.environ.get('PIBOX_PI_WEB_PORT') or PI_WEB_PORT}:{PI_WEB_PORT}"]
    if opts.unsafe_host_wayland:
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["WAYLAND_DISPLAY"] = os.environ["WAYLAND_DISPLAY"]
        env["XDG_SESSION_TYPE"] = "wayland"
    if opts.unsafe_enable_docker:
        env["DOCKER_HOST"] = f"unix://{DOCKER_RUN_DIR}/docker.sock"

    # Resolve the final volume list.
    # Priority: defaults < user-provided < workdir. A later entry for the
    # same destination wins, but keeps the position of the first one.
    # Host paths that do not exist are not mounted, so that Docker does not
    # create them as root.
    volumes = {}
    container_home = f"/home/{HOST_USER}"

    def add_default(path, dest, mode):
        if path.exists():
            volumes[dest] = f"{path}:{dest}:{mode}"

    add_default(HOME / ".pi", f"{container_home}/.pi", "rw")
    # extensions are read-only at runtime
    add_default(HOME / ".pi/agent/extensions", f"{container_home}/.pi/agent/extensions", "ro")
    if opts.harness == "claude":
        add_default(HOME / ".claude", f"{container_home}/.claude", "rw")
        # claude really hates to have its config file read-only
        add_default(HOME / ".claude.json", f"{container_home}/.claude.json", "rw")
    add_default(HOME / ".gitconfig", f"{container_home}/.gitconfig", "ro")
    volumes[f"{container_home}/.cache"] = f"pibox-cache:{container_home}/.cache:rw"
    ca_bundle = host_ca_bundle()
    if ca_bundle:
        volumes["/etc/ssl/host-ca-bundle.pem"] = f"{ca_bundle}:/etc/ssl/host-ca-bundle.pem:ro"
        env["SSL_CERT_FILE"] = "/etc/ssl/host-ca-bundle.pem"
    if opts.unsafe_enable_aws:
        add_default(HOME / ".aws", f"{container_home}/.aws", "ro")
    if opts.unsafe_enable_kube:
        add_default(HOME / ".kube", f"{container_home}/.kube", "ro")
    if opts.unsafe_enable_gcx:
        add_default(HOME / ".config/gcx", f"{container_home}/.config/gcx", "ro")
    if opts.unsafe_host_wayland:
        volumes[str(wayland_socket)] = f"{wayland_socket}:{wayland_socket}:rw"
    for dest, spec in user_volumes:
        volumes[dest] = spec
    # When launched from $HOME, the workdir is read-only to avoid exposing the
    # whole home directory rw. An explicit --volume for $HOME wins, unless
    # --read-only is given.
    if opts.read_only or not (workdir == HOME and str(HOME) in volumes):
        read_only = opts.read_only or workdir == HOME
        volumes[str(workdir)] = f"{workdir}:{workdir}:{'ro,' if read_only else ''}rslave"
    for spec in volumes.values():
        args += ["-v", spec]

    # user variables come last so they override the ones set by pibox
    args += [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]
    args += [arg for spec in opts.env for arg in ("-e", spec)]

    tty = sys.stdin.isatty() and sys.stdout.isatty()
    if tty:
        args += ["-it"]

    if opts.unsafe_enable_docker and opts.pull:
        run(["docker", "pull", DIND_IMAGE])
    sidecar = None
    try:
        # The agent container is created with the lock held, so that a
        # session that exits does not remove a sidecar this one is about to use.
        with lock_sessions():
            if opts.unsafe_enable_docker:
                sidecar = docker_sidecar(workdir, volumes[str(workdir)], ca_bundle)
                args = ["--label", f"pibox.docker-sidecar={sidecar}", "-v", f"{sidecar}:{DOCKER_RUN_DIR}", *args]
            cmd = [
                "docker", "create", "--rm", "--init",
                "--name", name,
                "-w", str(workdir),
                "--ipc=none",
                "--pids-limit=1024",
                *args,
                image, *agent_args,
            ]
            print(f"+ {shlex.join(cmd)}", file=sys.stderr)
            if subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode != 0:
                die("could not create the agent container", 1)
        return subprocess.run(["docker", "start", "-a", *(["-i"] if tty else []), name]).returncode
    finally:
        with lock_sessions():
            # --rm cannot clean up when the docker CLI itself is killed
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            if sidecar:
                users = subprocess.run(["docker", "ps", "-a", "-q", "--filter", f"label=pibox.docker-sidecar={sidecar}"],
                                       capture_output=True, text=True).stdout.split()
                if not users:
                    subprocess.run(["docker", "rm", "-f", sidecar], capture_output=True)
                    remove_volume(sidecar)


def exit_on_signal(signum, _frame):
    """Turn a termination signal into SystemExit so cleanup still runs."""
    sys.exit(128 + signum)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, exit_on_signal)
    signal.signal(signal.SIGHUP, exit_on_signal)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
