#!/usr/bin/env python3
"""Run an AI coding agent (pi or Claude Code) in an OpenShell sandbox.

The working directory is mounted into the sandbox at the same path, and the
agent runs as the host user so files keep their ownership.
"""

import argparse
import configparser
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

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
# Sandbox policy used when neither --policy nor OPENSHELL_SANDBOX_POLICY is set.
DEFAULT_POLICY = HOME / ".config" / "openshell" / "policy.yaml"
# Socket directory of the --unsafe-enable-docker sidecar, at the same path in
# the sidecar and the sandbox. It must be outside /run, which rootlesskit
# copies up into its own mount namespace.
DOCKER_RUN_DIR = "/pibox-docker"
# Generated kubeconfig that points at the host kubectl proxies.
KUBECONFIG_PATH = "/etc/pibox/kubeconfig"


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
    p.add_argument("-r", "--read-only", "--ro", action="store_true", help="mount the workdir read-only")
    p.add_argument("-v", "--volume", action="append", default=[],
                   help="mount a volume (repeatable; Docker -v syntax); "
                        "PATH or PATH:ro|rw mounts PATH at the same path")
    p.add_argument("-e", "--env", action="append", default=[],
                   help="set an environment variable (repeatable; Docker -e syntax)")
    p.add_argument("-P", "--extra-package", action="append", default=[],
                   help="install an extra pacman or AUR package in the image (repeatable)")
    p.add_argument("-p", "--port", action="append", default=[],
                   help="forward a sandbox port to the same host port (repeatable; [IP:]PORT); "
                        f"pi-web listens on {PI_WEB_PORT} unless PIBOX_PI_WEB_PORT is set")
    p.add_argument("--policy", help="OpenShell policy file (default: $OPENSHELL_SANDBOX_POLICY, "
                                     "or ~/.config/openshell/policy.yaml)")
    p.add_argument("--unsafe-enable-docker", action="store_true",
                   help="start a rootless Docker daemon in a privileged sidecar container")
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


def mount(source, read_only=False, rslave=False):
    """Return a mount entry for the mount table.

    Args:
        source: absolute host path for a bind mount, or volume name for a
            named volume.
        read_only: mount read-only.
        rslave: propagate host mounts made under a bind source after launch.
    """
    return {"source": str(source), "read_only": read_only, "rslave": rslave}


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
    return target, mount(source, read_only, rslave)


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
        "docker", "build", "-q", "-t", tag,
        "--build-arg", f"BASE_IMAGE={image}",
        "--build-arg", f"HOST_UID={os.getuid()}",
        "--build-arg", f"HOST_GID={os.getgid()}",
        "--build-arg", f"HOST_USER={HOST_USER}",
        "--build-arg", f"HOST_HOME={HOME}",
        "--build-arg", f"EXTRA_PACKAGES={' '.join(extra_packages)}",
        "-",
    ], input=USER_DOCKERFILE, text=True, stdout=subprocess.DEVNULL)
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


def upsert_profile(profile):
    """Import an OpenShell provider profile, or update it if it already exists.

    Args:
        profile: profile dict, without resource_version.
    """
    current = subprocess.run(["openshell", "profile", "export", profile["id"], "-o", "json"],
                             capture_output=True, text=True)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as f:
        if current.returncode == 0:
            profile = {**profile, "resource_version": json.loads(current.stdout)["resource_version"]}
            cmd = ["openshell", "profile", "update", profile["id"], "-f", f.name]
        else:
            cmd = ["openshell", "profile", "import", "-f", f.name]
        yaml.safe_dump(profile, f)
        f.flush()
        run(cmd, stdout=subprocess.DEVNULL)


def upsert_provider(name, profile_id, credentials):
    """Create an OpenShell provider, or replace its credentials if it exists.

    A provider of another type or with other credential keys is deleted and
    created again.

    The values go through the environment of the openshell CLI so that they
    do not appear in its arguments.

    Args:
        name: provider name.
        profile_id: provider profile (type) of the provider.
        credentials: dict of credential environment key to value.
    """
    current = subprocess.run(["openshell", "provider", "get", name], capture_output=True, text=True)
    exists = current.returncode == 0
    keys = ", ".join(sorted(credentials))
    if exists and not (re.search(rf"^\s*Type:\s*{re.escape(profile_id)}\s*$", current.stdout, re.M)
                       and re.search(rf"^\s*Credential keys:\s*{re.escape(keys)}\s*$", current.stdout, re.M)):
        # the type and the credential keys of a provider cannot be updated
        run(["openshell", "provider", "delete", name], stdout=subprocess.DEVNULL)
        exists = False
    cmd = ["openshell", "provider", "update", name] if exists else \
        ["openshell", "provider", "create", "--name", name, "--type", profile_id]
    for key in credentials:
        cmd += ["--credential", key]
    run(cmd, env={**os.environ, **credentials}, stdout=subprocess.DEVNULL)


def setup_aws(image):
    """Expose the host AWS profile to the sandbox through SigV4 signing.

    Every profile in ~/.aws/credentials is stored in its own provider, but
    only the profile named by the host AWS_PROFILE (default: "default") is
    used by the sandbox. OpenShell signs with fixed AWS_* keys, so a sandbox
    cannot hold more than one AWS provider. The sandbox only gets
    placeholders; the OpenShell proxy re-signs every AWS request with the real
    credentials, so the IAM policy of the profile controls access. The
    provider profile lists the AWS endpoints, so attaching the provider adds
    their network rule to the sandbox.

    Args:
        image: per-user image, whose AWS CLI lists the AWS services.

    Returns:
        A tuple (provider name, region), or None when the profile is not found.
    """
    profile = os.environ.get("AWS_PROFILE") or "default"
    config = configparser.RawConfigParser()
    config.read(HOME / ".aws" / "credentials")
    sections = [s for s in config.sections() if config.has_option(s, "aws_access_key_id")]
    if not sections:
        return None

    out = run(["docker", "run", "--rm", "-i", "--entrypoint", "python3", image, "-"],
              input=AWS_ENDPOINTS_SCRIPT, capture_output=True, text=True)
    generated = json.loads(out.stdout)
    upsert_profile({
        "id": "pibox-aws",
        "display_name": "pibox AWS",
        "description": "AWS credentials from the host ~/.aws/credentials, used for SigV4 signing",
        "category": "other",
        "credentials": [
            {"name": "access_key_id", "env_vars": ["AWS_ACCESS_KEY_ID"], "required": True},
            {"name": "secret_access_key", "env_vars": ["AWS_SECRET_ACCESS_KEY"], "required": True},
            {"name": "session_token", "env_vars": ["AWS_SESSION_TOKEN"], "required": False},
        ],
        "endpoints": [{"host": e["host"], "port": 443, "protocol": "rest", "access": "full",
                       "credential_signing": "sigv4", **{k: v for k, v in e.items() if k != "host"}}
                      for e in generated["endpoints"]],
        "binaries": [generated["interpreter"]],
    })
    names = {s: "pibox-aws-" + re.sub(r"[^a-z0-9-]", "-", s.lower()) for s in sections}
    for section, name in names.items():
        credentials = {"AWS_ACCESS_KEY_ID": config[section]["aws_access_key_id"],
                       "AWS_SECRET_ACCESS_KEY": config[section]["aws_secret_access_key"]}
        if config[section].get("aws_session_token"):
            credentials["AWS_SESSION_TOKEN"] = config[section]["aws_session_token"]
        upsert_provider(name, "pibox-aws", credentials)
    if profile not in names:
        return None
    return names[profile], config[profile].get("region")


def setup_gcx():
    """Expose the host gcx stacks to the sandbox through bearer token injection.

    Each stack token from ~/.config/gcx/config.yaml is stored in its own
    provider, with its own provider profile that lists the Grafana host of
    the stack, but only the stack of the current context is used by the
    sandbox. The provider sets GRAFANA_TOKEN to a placeholder, and the
    returned GRAFANA_* variables select the server of the stack.

    Returns:
        A tuple (provider name, dict of GRAFANA_* environment variables), or
        None when the current context has no stack with a token.
    """
    try:
        config = yaml.safe_load((HOME / ".config" / "gcx" / "config.yaml").read_text())
    except FileNotFoundError:
        return None
    stacks = {stack: entry["grafana"] for stack, entry in (config.get("stacks") or {}).items()
              if ((entry or {}).get("grafana") or {}).get("token") and entry["grafana"].get("server")}
    if not stacks:
        return None

    names = {s: "pibox-gcx-" + re.sub(r"[^a-z0-9-]", "-", s.lower()) for s in stacks}
    for stack, name in names.items():
        url = urlsplit(stacks[stack]["server"])
        upsert_profile({
            "id": name,
            "display_name": f"pibox gcx {stack}",
            "description": f"Grafana token of the gcx stack {stack} from the host gcx configuration",
            "category": "other",
            "credentials": [{"name": "token", "env_vars": ["GRAFANA_TOKEN"], "required": True,
                             "auth_style": "bearer", "header_name": "authorization"}],
            "endpoints": [{"host": url.hostname, "port": url.port or 443, "protocol": "rest", "access": "full"}],
            # The image gcx, or a host build from a mounted ~/go, which comes first in PATH.
            "binaries": ["/usr/local/bin/gcx", f"{HOME}/go/bin/gcx"],
        })
        upsert_provider(name, name, {"GRAFANA_TOKEN": stacks[stack]["token"]})

    context = (config.get("contexts") or {}).get(config.get("current-context")) or {}
    stack = context.get("stack")
    if stack not in names:
        return None
    grafana = stacks[stack]
    env = {"GRAFANA_SERVER": grafana["server"]}
    for key in ["org-id", "stack-id"]:
        if grafana.get(key):
            env["GRAFANA_" + key.upper().replace("-", "_")] = str(grafana[key])
    return names[stack], env


def setup_kube(work_dir):
    """Expose the host kubectl contexts to the sandbox through kubectl proxy.

    One kubectl proxy per context runs on the host loopback, on a port picked
    by the kernel, and authenticates with the host kubeconfig. The sandbox
    gets a kubeconfig without credentials that points at the proxies through
    host.openshell.internal.

    Args:
        work_dir: directory for the generated kubeconfig and the proxy logs.

    Returns:
        A tuple (kubeconfig path, proxy ports, proxy processes), or None when
        there is no kubectl context on the host.
    """
    if not shutil.which("kubectl"):
        return None
    view = subprocess.run(["kubectl", "config", "view", "-o", "json"], capture_output=True, text=True)
    config = json.loads(view.stdout or "{}") if view.returncode == 0 else {}
    contexts = {c["name"]: c.get("context") or {} for c in config.get("contexts") or []}
    if not contexts:
        return None

    procs, ports = [], {}
    for name in contexts:
        log = Path(work_dir) / f"kubectl-proxy-{len(procs)}.log"
        with open(log, "w") as out:
            # exec and attach are rejected by default
            procs.append(subprocess.Popen(
                ["kubectl", "proxy", "--context", name, "--address", "127.0.0.1", "--port", "0",
                 "--accept-hosts", r"^host\.openshell\.internal$", "--reject-paths", "^$"],
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT))
        for _ in range(100):
            m = re.search(r"Starting to serve on [^\s]*:(\d+)", log.read_text())
            if m or procs[-1].poll() is not None:
                break
            time.sleep(0.1)
        if m:
            ports[name] = int(m[1])
        else:
            print(f"pibox: warning: kubectl proxy failed for context {name}: {log.read_text().strip()}",
                  file=sys.stderr)

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": name, "cluster": {"server": f"http://host.openshell.internal:{port}"}}
                     for name, port in ports.items()],
        "users": [{"name": "kubectl-proxy", "user": {}}],
        "contexts": [{"name": name, "context": {"cluster": name, "user": "kubectl-proxy",
                                               **({"namespace": contexts[name]["namespace"]}
                                                  if contexts[name].get("namespace") else {})}}
                     for name in ports],
    }
    if config.get("current-context") in ports:
        kubeconfig["current-context"] = config["current-context"]
    path = Path(work_dir) / "kubeconfig"
    path.write_text(yaml.safe_dump(kubeconfig, sort_keys=False))
    return path, sorted(ports.values()), procs


def add_kube_rule(sandbox_name, ports, stop):
    """Allow kubectl to reach the kubectl proxies once the sandbox exists.

    The proxy ports change with each session, so the rule is added to the
    running sandbox instead of the policy file. OpenShell names the rules,
    because --rule-name accepts only one endpoint. Retries until it succeeds
    or stop is set.

    Args:
        sandbox_name: name of the sandbox.
        ports: kubectl proxy ports on the host.
        stop: threading.Event that ends the retries.
    """
    cmd = ["openshell", "policy", "update", sandbox_name, "--binary", "/usr/bin/kubectl", "--wait"]
    for port in ports:
        cmd += ["--add-endpoint", f"host.openshell.internal:{port}"]
    while not stop.wait(1):
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            return


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
    user_mounts = [parse_volume(v) for v in opts.volume]
    user_env = dict(parse_env(e) for e in opts.env)
    policy_file = Path(opts.policy or os.environ.get("OPENSHELL_SANDBOX_POLICY") or DEFAULT_POLICY).expanduser()
    if not policy_file.is_file():
        die(f"policy file not found: {policy_file} (see contrib/openshell/policy.yaml)")
    ports = [parse_port(p) for p in opts.port]

    skip = opts.sac_moe_patience
    if opts.unsafe_enable_docker:
        confirm("--unsafe-enable-docker starts a privileged container, "
                "and the network policy does not apply to the containers it runs", skip)
    if opts.unsafe_host_wayland:
        confirm("--unsafe-host-wayland mounts the Wayland socket into the sandbox", skip)
        if not os.environ.get("WAYLAND_DISPLAY"):
            die("--unsafe-host-wayland requires WAYLAND_DISPLAY to be set")
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        wayland_socket = Path(runtime_dir) / os.environ["WAYLAND_DISPLAY"]
        if not wayland_socket.is_socket():
            die(f"Wayland socket not found: {wayland_socket}")
    workdir = Path.cwd().resolve()

    status = subprocess.run(["openshell", "status", "-o", "json"], capture_output=True, text=True)
    if status.returncode != 0 or json.loads(status.stdout or "{}").get("status") != "connected":
        die("the OpenShell gateway is not reachable (see contrib/openshell/gateway.toml)", 1)

    # ensure these exist on the host so Docker does not create them as root
    (HOME / ".pi").mkdir(exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if opts.harness == "claude":
        (HOME / ".claude").mkdir(exist_ok=True)
        (HOME / ".claude.json").touch()
    sandbox_name = f"pibox-{os.getpid()}"  # openshell limits names to 19 characters
    rslave_volumes = []
    docker_sidecar = None
    kube_procs = []
    stop_threads = threading.Event()
    forward_thread = None
    tmp = tempfile.TemporaryDirectory(dir=STATE_DIR, prefix="tmp.")
    try:
        image = build_images(opts.harness, opts.build, opts.pull)
        user_image = build_user_image(opts.harness, image, opts.extra_package)

        aws_provider, aws_region = setup_aws(user_image) or (None, None)
        gcx_provider, gcx_env = setup_gcx() or (None, {})
        providers = [p for p in (aws_provider, gcx_provider) if p]
        kubeconfig, kube_ports, kube_procs = setup_kube(tmp.name) or (None, [], [])

        # Resolve the final mount list.
        # Priority: defaults < user-provided < workdir. A later entry for the
        # same target wins, but keeps the position of the first one.
        mounts = {
            HOME / ".pi": mount(HOME / ".pi"),
            HOME / ".cache": mount("pibox-cache"),
        }
        if opts.harness == "claude":
            mounts[HOME / ".claude"] = mount(HOME / ".claude")
            # claude really hates to have its config file read-only
            mounts[HOME / ".claude.json"] = mount(HOME / ".claude.json")
        if (HOME / ".gitconfig").is_file():
            mounts[HOME / ".gitconfig"] = mount(HOME / ".gitconfig", read_only=True)
        if kubeconfig:
            mounts[Path(KUBECONFIG_PATH)] = mount(kubeconfig, read_only=True)
        if opts.unsafe_host_wayland:
            mounts[wayland_socket] = mount(wayland_socket)
        if opts.unsafe_enable_docker:
            docker_run_dir = Path(tmp.name) / "docker"
            docker_run_dir.mkdir()
            mounts[Path(DOCKER_RUN_DIR)] = mount(docker_run_dir)
        for target, entry in user_mounts:
            mounts[Path(target)] = entry
        # When launched from $HOME, the workdir is read-only to avoid exposing
        # the whole home directory rw. An explicit --volume for $HOME wins,
        # unless --read-only is given.
        home_volume = workdir == HOME and HOME in mounts
        if opts.read_only or not home_volume:
            mounts[workdir] = mount(workdir, read_only=opts.read_only or workdir == HOME, rslave=True)

        driver_mounts = []
        for target, entry in mounts.items():
            source = entry["source"]
            driver_mount = {"type": "bind", "source": source, "target": str(target),
                            "read_only": entry["read_only"]}
            if not source.startswith("/"):
                # docker run creates missing named volumes; the openshell driver does not
                run(["docker", "volume", "create", source], stdout=subprocess.DEVNULL)
                driver_mount["type"] = "volume"
            elif source == "/tmp" or source.startswith("/tmp/"):
                die(f"cannot mount {source}: the OpenShell gateway runs with a private /tmp")
            elif not Path(source).exists():
                die(f"cannot mount {source}: no such file or directory")
            elif entry["rslave"]:
                # OpenShell bind mounts are always rprivate; a local volume
                # with rslave lets host mounts made under the source after
                # launch appear in the sandbox.
                name = f"{sandbox_name}-{len(rslave_volumes)}"
                run(["docker", "volume", "create", "--driver", "local", "--opt", "type=none",
                     "--opt", f"device={source}", "--opt", "o=rbind,rslave", name], stdout=subprocess.DEVNULL)
                rslave_volumes.append(name)
                driver_mount.update(type="volume", source=name)
            driver_mounts.append(driver_mount)

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
        if kubeconfig:
            env["KUBECONFIG"] = KUBECONFIG_PATH
        env.update(gcx_env)
        if aws_provider:
            env["AWS_DEFAULT_REGION"] = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
                                         or aws_region)
        if opts.harness == "pi-web":
            # openshell forwards to the same port number, so listen on the host port
            env["PI_WEB_HOSTNAME"] = "127.0.0.1"
            if ports:
                env["PORT"] = PI_WEB_PORT
            else:
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
            *[arg for name in providers for arg in ("--provider", name)],
            "--tty" if tty else "--no-tty",
            *[arg for key, value in env.items() if value is not None for arg in ("--env", f"{key}={value}")],
            "--", "bash", "-c", script, "pibox", str(workdir), *HARNESS_COMMANDS[opts.harness], *agent_args,
        ]
        if opts.unsafe_enable_docker:
            docker_sidecar = f"{sandbox_name}-docker"
            start_docker_sidecar(docker_sidecar, user_image, workdir, docker_run_dir)
        if ports:
            # --forward cannot be combined with --no-keep
            forward_thread = threading.Thread(target=keep_forwards, args=(sandbox_name, ports, stop_threads),
                                              daemon=True)
            forward_thread.start()
        if kube_ports:
            threading.Thread(target=add_kube_rule, args=(sandbox_name, kube_ports, stop_threads), daemon=True).start()
        print(f"+ {shlex.join(cmd)}", file=sys.stderr)
        return subprocess.run(cmd).returncode
    finally:
        stop_threads.set()
        if forward_thread:
            # so that it does not restart a forward stopped below
            forward_thread.join()
        for spec in ports:
            subprocess.run(["openshell", "forward", "stop", spec.rsplit(":", 1)[-1], sandbox_name],
                           capture_output=True)
        # --no-keep cannot clean up when the openshell CLI itself is killed
        subprocess.run(["openshell", "sandbox", "delete", sandbox_name], capture_output=True)
        for proc in kube_procs:
            proc.terminate()
            proc.wait()
        if docker_sidecar:
            subprocess.run(["docker", "rm", "-f", docker_sidecar], capture_output=True)
        for name in rslave_volumes:
            remove_volume(name)
        tmp.cleanup()


# ---------------------------------------------------------------------------
# Embedded files. They live here so that a copy of launch.py works on its own.
# ---------------------------------------------------------------------------

# Per-user image, built by build_user_image() with this text on stdin.
USER_DOCKERFILE = r'''# Per-user layer built by launch.py on top of a harness image.
# Mirrors the host user so bind-mounted files keep their ownership, and
# installs --extra-package requests. Built without a context.
ARG BASE_IMAGE=ghcr.io/badjware/pibox:pi
FROM ${BASE_IMAGE}

ARG HOST_UID
ARG HOST_GID
ARG HOST_USER
ARG HOST_HOME
ARG EXTRA_PACKAGES=""

# Pre-create the parents of bind-mount targets so Docker does not create them as root.
RUN (getent group "$HOST_GID" >/dev/null || groupadd -g "$HOST_GID" "$HOST_USER") \
    && (getent passwd "$HOST_UID" >/dev/null || useradd -u "$HOST_UID" -g "$HOST_GID" -d "$HOST_HOME" -s /bin/bash "$HOST_USER") \
    && install -d -o "$HOST_UID" -g "$HOST_GID" \
        "$HOST_HOME" "$HOST_HOME/.cache" "$HOST_HOME/.config" "$HOST_HOME/.local" "$HOST_HOME/.local/share"

# Rootless dockerd for --unsafe-enable-docker: subordinate IDs for newuidmap /
# newgidmap, and their file capabilities, which can be lost in image export.
RUN echo "$HOST_USER:100000:65536" >> /etc/subuid \
    && echo "$HOST_USER:100000:65536" >> /etc/subgid \
    && setcap cap_setuid+ep /usr/bin/newuidmap \
    && setcap cap_setgid+ep /usr/bin/newgidmap

COPY --chmod=755 <<'EOF' /usr/local/lib/pibox/install-extra-packages.sh
#!/bin/bash
# Install --extra-package requests while building the per-user image.
#
# Usage: install-extra-packages.sh BUILD_USER PACKAGE...
#
# Runs as root. Official-repo packages are installed directly with pacman.
# Anything pacman does not know about is treated as an AUR package: we clone
# it, install its repo dependencies, build it as BUILD_USER (makepkg refuses
# to run as root), then install the built package with pacman -U.
#
# AUR builds are not recursive: only official-repo dependencies are resolved,
# so an AUR package with AUR-only dependencies will fail to build.
set -e

build_user="$1"
shift

build_aur_package() {
    # Build a single AUR package as build_user and install it as root.
    local pkg="$1"
    local build_dir
    build_dir=$(mktemp -d)
    chown "$build_user:" "$build_dir"

    runuser -u "$build_user" -- git clone --depth=1 "https://aur.archlinux.org/$pkg.git" "$build_dir/$pkg"

    # Install repo dependencies as root so makepkg can run without sudo.
    local deps
    deps=$(runuser -u "$build_user" -- makepkg --printsrcinfo -D "$build_dir/$pkg" \
        | sed -n 's/^[[:space:]]*\(make\)\?depends = //p' | sed 's/[<>=].*//')
    if [[ -n "$deps" ]]; then
        pacman -S --noconfirm --needed --asdeps $deps
    fi

    runuser -u "$build_user" -- bash -c "cd '$build_dir/$pkg' && makepkg --noconfirm"

    local built
    built=$(find "$build_dir/$pkg" -name '*.pkg.tar.zst' | grep -v -- '-debug')
    pacman -U --noconfirm $built
    rm -rf "$build_dir"
}

# Arch does not support partial upgrades: packages from a fresh sync may need
# newer libraries than the image has.
pacman -Syu --noconfirm >/dev/null

repo_packages=()
aur_packages=()
for pkg in "$@"; do
    if pacman -Si "$pkg" >/dev/null 2>&1; then
        repo_packages+=("$pkg")
    else
        aur_packages+=("$pkg")
    fi
done

[[ ${#repo_packages[@]} -gt 0 ]] && pacman -S --noconfirm --needed "${repo_packages[@]}"

if [[ ${#aur_packages[@]} -gt 0 ]]; then
    pacman -S --noconfirm --needed base-devel git
    for pkg in "${aur_packages[@]}"; do
        build_aur_package "$pkg"
    done
fi

pacman -Scc --noconfirm
EOF
RUN if [ -n "$EXTRA_PACKAGES" ]; then \
        /usr/local/lib/pibox/install-extra-packages.sh "$HOST_USER" $EXTRA_PACKAGES; \
    fi

ENV PATH=${HOST_HOME}/go/bin:${PATH}
# billion-context (pi package) defaults to XDG paths, which are not persisted.
# Keep its config, sessions, and log under ~/.pi, which is bind-mounted.
ENV BILI_CONFIG_FILE=${HOST_HOME}/.pi/agent/billion-context/billion-context.json \
    BILI_SESSIONS_DIR=${HOST_HOME}/.pi/agent/billion-context/sessions \
    ACP_LOG_FILE=${HOST_HOME}/.pi/agent/billion-context/bili.log

USER ${HOST_UID}:${HOST_GID}
'''

# Piped to python3 in the per-user image by setup_aws().
AWS_ENDPOINTS_SCRIPT = r'''"""Print the AWS endpoints that need SigV4 signing, as JSON.

Run by launch.py with the Python interpreter of the pibox image, which has the
AWS CLI and its botocore service models. Prints an object with:

    interpreter: real path of this interpreter, which runs aws and boto3
    endpoints:   list of {host, signing_service[, signing_region]}
"""

import glob
import gzip
import json
import os
import sys

import awscli  # noqa: F401  AWS CLI v2 bundles botocore and makes it importable
import botocore

endpoints = {}
for path in glob.glob(os.path.join(os.path.dirname(botocore.__file__), "data", "*", "*", "service-2.json*")):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as f:
        meta = json.load(f)["metadata"]
    auth = meta.get("auth", [])
    if meta.get("signatureVersion") not in ("v4", "s3v4") and not any(a.endswith("#sigv4") for a in auth):
        continue
    prefix = meta["endpointPrefix"]
    name = meta.get("signingName") or prefix
    # the proxy reads the region from regional host names
    endpoints[f"{prefix}.*.amazonaws.com"] = (name, None)
    if meta.get("globalEndpoint"):
        endpoints[meta["globalEndpoint"]] = (name, "us-east-1")

# Virtual-hosted S3 buckets. The legacy global form (**.s3.amazonaws.com) is
# left out: OpenShell rejects it next to this one because their signing
# regions differ and the wildcards may overlap.
endpoints["**.s3.*.amazonaws.com"] = ("s3", None)

print(json.dumps({
    "interpreter": os.path.realpath(sys.executable),
    "endpoints": [{"host": host, "signing_service": name, **({"signing_region": region} if region else {})}
                  for host, (name, region) in sorted(endpoints.items())],
}))
'''


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
