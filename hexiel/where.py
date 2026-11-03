"""Where this hexiel is running, when that is not simply "a local terminal": inside a docker/podman
container, a Kubernetes pod, WSL, or an SSH session. Shown as chips in the sidebar so a window
that is attached to a server or a container is never mistaken for the local machine."""

import os
import re
import socket
from pathlib import Path


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return ""


def detect(env=None, read=_read, hostname=_hostname, exists=os.path.exists) -> list[tuple[str, str]]:
    """[(kind, name)] outermost first: kind is 'ssh' | 'docker' | 'podman' | 'k8s' | 'lxc' | 'wsl' | 'container'.
    name is the most useful label for it: the container name, pod name, the machine's host name for ssh."""
    env = os.environ if env is None else env
    found: list[tuple[str, str]] = []
    host = hostname()
    if env.get("WSL_DISTRO_NAME"):
        found.append(("wsl", env["WSL_DISTRO_NAME"]))
    if env.get("SSH_CONNECTION") or env.get("SSH_CLIENT") or env.get("SSH_TTY"):
        found.append(("ssh", host))
    cgroup = read("/proc/1/cgroup") + read("/proc/self/cgroup")
    podman_file = read("/run/.containerenv") if exists("/run/.containerenv") else None
    if env.get("KUBERNETES_SERVICE_HOST"):
        found.append(("k8s", env.get("POD_NAME") or host))
    elif podman_file is not None or env.get("container") == "podman":
        m = re.search(r'^name="([^"]+)"', podman_file or "", re.M)
        found.append(("podman", m.group(1) if m else host))
    elif exists("/.dockerenv") or re.search(r"docker|containerd|libpod", cgroup):
        found.append(("docker", env.get("DEVCONTAINER_NAME") or host))
    elif env.get("container") in ("lxc", "lxc-libvirt"):
        found.append(("lxc", host))
    elif env.get("container"):
        found.append(("container", host))
    return found


_cache: list[tuple[str, str]] | None = None


def where() -> list[tuple[str, str]]:
    """detect(), once per process: the answer cannot change while hexiel runs."""
    global _cache
    if _cache is None:
        _cache = detect()
    return _cache
