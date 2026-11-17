#!/usr/bin/env python3
"""Run an AI coding agent (pi or Claude Code) in an OpenShell sandbox.

The working directory is mounted into the sandbox at the same path, and the
agent runs as the host user so files keep their ownership.
"""

import argparse
import json
import os
import pwd
import shlex
import signal
import subprocess
import sys
import tempfile
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
                   help="bind-mount an extra volume (repeatable); PATH or PATH:ro|rw mounts PATH at the same path")
    p.add_argument("-m", "--mount", action="append", default=[], help="add a mount (repeatable)")
    p.add_argument("-e", "--env", action="append", default=[],
                   help="set an environment variable (repeatable; Docker -e syntax)")
    p.add_argument("-P", "--extra-package", action="append", default=[],
                   help="install an extra pacman or AUR package in the image (repeatable)")
    p.add_argument("-p", "--port", action="append", default=[],
                   help="publish a container port (repeatable; Docker -p syntax); "
                        "pi-web listens on 30141; default 127.0.0.1:30141:30141")
    p.add_argument("--unsafe-enable-docker", action="store_true", help="enable rootless Docker-in-Docker (privileged)")
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


def main():
    """Entry point. Returns the exit status of the agent."""
    opts, agent_args = parse_args(sys.argv[1:])

    # TODO(openshell): port the remaining options, then remove this check.
    unported = [flag for flag, used in [
        (f"--harness {opts.harness}", opts.harness != "pi"),
        ("--volume", opts.volume), ("--mount", opts.mount), ("--env", opts.env), ("--port", opts.port),
        ("--unsafe-enable-docker", opts.unsafe_enable_docker), ("--unsafe-enable-aws", opts.unsafe_enable_aws),
        ("--unsafe-enable-kube", opts.unsafe_enable_kube), ("--unsafe-enable-gcx", opts.unsafe_enable_gcx),
        ("--unsafe-host-wayland", opts.unsafe_host_wayland),
    ] if used]
    if unported:
        die(f"not yet supported with OpenShell: {' '.join(unported)}")

    skip = opts.sac_moe_patience
    if os.getuid() == 0:
        confirm("running as root", skip)
    if opts.unsafe_enable_docker:
        confirm("--unsafe-enable-docker enables privileged mode", skip)
    if opts.unsafe_enable_aws:
        confirm("--unsafe-enable-aws mounts ~/.aws into the sandbox", skip)
    if opts.unsafe_enable_kube:
        confirm("--unsafe-enable-kube mounts ~/.kube into the sandbox", skip)
    if opts.unsafe_enable_gcx:
        confirm("--unsafe-enable-gcx mounts ~/.config/gcx into the sandbox", skip)
    if opts.unsafe_host_wayland:
        confirm("--unsafe-host-wayland mounts the Wayland socket into the sandbox", skip)
    for port in opts.port:
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

    sandbox_name = f"pibox-{opts.harness}-{os.getpid()}"
    workdir_volume = None
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

        # target -> (type, source, read_only); a later entry for the same target wins
        mounts = {}
        mounts[HOME / ".pi"] = ("bind", HOME / ".pi", False)
        mounts[HOME / ".pi/agent/extensions"] = ("bind", HOME / ".pi/agent/extensions", True)  # read-only at runtime
        if (HOME / ".gitconfig").is_file():
            mounts[HOME / ".gitconfig"] = ("bind", HOME / ".gitconfig", True)
        run(["docker", "volume", "create", "pibox-cache"], stdout=subprocess.DEVNULL)
        mounts[HOME / ".cache"] = ("volume", "pibox-cache", False)
        for path in map(Path, ["/etc/fonts", "/usr/share/fonts", "/var/cache/fontconfig"]):
            if path.is_dir():
                mounts[path] = ("bind", path, True)

        # The workdir goes through a local volume because OpenShell bind mounts
        # are always rprivate; rslave lets host mounts made under the workdir
        # after launch appear in the sandbox. When launched from $HOME, the
        # workdir is read-only to avoid exposing the whole home directory rw.
        run(["docker", "volume", "create", "--driver", "local", "--opt", "type=none",
             "--opt", f"device={workdir}", "--opt", "o=rbind,rslave", f"pibox-workdir-{sandbox_name}"],
            stdout=subprocess.DEVNULL)
        workdir_volume = f"pibox-workdir-{sandbox_name}"
        mounts[workdir] = ("volume", workdir_volume, opts.read_only or workdir == HOME)

        driver_mounts = []
        fs_policy = {"read_only": [], "read_write": [str(HOME)]}
        for target, (kind, source, read_only) in mounts.items():
            driver_mounts.append({"type": kind, "source": str(source), "target": str(target), "read_only": read_only})
            fs_policy["read_only" if read_only else "read_write"].append(str(target))

        policy = merge_policies([
            yaml.safe_load((SCRIPT_DIR / "common" / "policy.yaml").read_text()),
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

        tty = sys.stdin.isatty() and sys.stdout.isatty()
        # Without a TTY, openshell never forwards EOF on stdin, so an agent
        # that reads stdin (pi -p) would wait forever.
        script = 'cd -- "$1" && shift && exec pi "$@"' + ("" if tty else " </dev/null")
        cmd = [
            "openshell", "sandbox", "create",
            "--name", sandbox_name,
            "--from", user_image,
            "--policy", str(policy_file),
            "--driver-config-json", json.dumps({"docker": {"mounts": driver_mounts}}),
            "--no-keep",
            "--no-auto-providers",
            "--tty" if tty else "--no-tty",
            *[arg for key, value in env.items() if value for arg in ("--env", f"{key}={value}")],
            "--", "bash", "-c", script, "pibox", str(workdir), *agent_args,
        ]
        print(f"+ {shlex.join(cmd)}", file=sys.stderr)
        return subprocess.run(cmd).returncode
    finally:
        # --no-keep cannot clean up when the openshell CLI itself is killed
        subprocess.run(["openshell", "sandbox", "delete", sandbox_name], capture_output=True)
        if workdir_volume:
            remove_volume(workdir_volume)
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
