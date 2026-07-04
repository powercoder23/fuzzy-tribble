"""
docker_control.py — container lifecycle control for the Settings page.

Parses docker-compose.prod.yml directly for the service list (iv-collector,
break-bounce, discount, convex-engine, dashboard, ...) rather than
hardcoding — add/remove a service in compose and the Settings page follows.

Requires (added in this PR):
  - /var/run/docker.sock mounted into the dashboard container
  - the repo root mounted read-only at /compose (for the compose file itself
    and build context — bind-mounting is enough, docker CLI uploads the
    context to the host daemon over the socket)
  - docker-ce-cli + docker-compose-plugin installed in the dashboard image
    (see Dockerfile.dashboard)

SECURITY: this gives the dashboard container root-equivalent control over
the whole NAS's Docker daemon via the socket. Acceptable for a Tailscale-only
single-user box; don't expose port 8050 beyond that without auth in front.
"""

import json
import subprocess
from pathlib import Path
from typing import Literal

COMPOSE_FILE = Path("/compose/docker-compose.prod.yml")
COMPOSE_PROJECT_DIR = COMPOSE_FILE.parent

Action = Literal["start", "stop", "restart", "rebuild"]


def _load_compose() -> dict:
    import yaml
    return yaml.safe_load(COMPOSE_FILE.read_text())


def list_services() -> list[str]:
    return list(_load_compose().get("services", {}).keys())


def list_service_profiles() -> dict[str, list[str]]:
    """{service_name: [profiles]}. momentum/directional-iv are already
    profile-gated (DISCONTINUED) — this just surfaces that in the UI."""
    return {name: spec.get("profiles", []) for name, spec in _load_compose().get("services", {}).items()}


def _run_compose(*args: str) -> dict:
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    result = subprocess.run(
        cmd, cwd=COMPOSE_PROJECT_DIR, capture_output=True, text=True, timeout=180,
    )
    return {
        "cmd": " ".join(cmd),
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "ok": result.returncode == 0,
    }


def container_action(service_name: str, action: Action) -> dict:
    services = list_services()
    if service_name not in services:
        raise ValueError(f"Unknown service '{service_name}'. Known: {services}")

    if action == "start":
        return _run_compose("start", service_name)
    if action == "stop":
        return _run_compose("stop", service_name)
    if action == "restart":
        return _run_compose("restart", service_name)
    if action == "rebuild":
        build_res = _run_compose("build", "--no-cache", service_name)
        if not build_res["ok"]:
            return build_res
        return _run_compose("up", "-d", "--force-recreate", service_name)
    raise ValueError(f"Unknown action '{action}'")


def bulk_action(service_names: list[str], action: Action) -> list[dict]:
    return [{"service": s, **container_action(s, action)} for s in service_names]


def compose_up_selected(service_names: list[str]) -> dict:
    """Start exactly the given services, ignoring `profiles:` gating
    (explicit service names always override profile exclusion) — this is
    the manual override the Settings page's startup checkboxes drive."""
    if not service_names:
        raise ValueError("No services given")
    return _run_compose("up", "-d", *service_names)


def get_running_status() -> dict[str, str]:
    result = _run_compose("ps", "--format", "json")
    if not result["ok"]:
        return {}
    status = {}
    for line in result["stdout"].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            status[entry.get("Service", entry.get("Name"))] = entry.get("State", "unknown")
        except json.JSONDecodeError:
            continue
    return status
