"""Boot throwaway routstr-core nodes for whole-system secret-lifecycle e2e (#553).

The standing compose ``node-a`` is one long-lived container with every secret
pre-supplied, so it cannot exercise the BOOT-TIME secret behaviours #553 adds:
refusing to start without ``ROUTSTR_SECRET_KEY``, generating + logging a
first-run admin password, bricking on a key change, and recovering an encrypted
nsec on a later boot after ``NSEC`` has left the env. Those need ephemeral nodes
booted with tailored env — two of them across a *pair* of boots sharing one
volume.

This module drives the *already-built* node image (the orchestrator builds it for
the standing stack) with ``docker run``, attached to the compose network so the
node reaches ``relay`` / ``mock-openai`` / the mints by service name, writing to a
throwaway volume that is removed afterwards. Nothing here touches the standing
``node-a`` container or its volume.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import httpx

from tests.integration.targets import is_remote, unavailable

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A throwaway node serves on this host port (the compose stack never binds it).
LIFECYCLE_PORT = 8077


def require_local_docker() -> None:
    """Skip/fail unless we can boot local containers (see targets.unavailable)."""
    if is_remote():
        unavailable(
            "secret-lifecycle e2e boots throwaway local containers; "
            "it does not apply to a remote target"
        )
    if shutil.which("docker") is None:
        unavailable("docker CLI required to boot ephemeral nodes")


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )


@lru_cache(maxsize=1)
def _project_name() -> str:
    proc = _compose("config", "--format", "json")
    if proc.returncode != 0:
        unavailable(
            f"could not read compose config: {(proc.stderr or proc.stdout)[:200]}"
        )
    try:
        return str(json.loads(proc.stdout)["name"])
    except (ValueError, KeyError) as exc:  # pragma: no cover - defensive
        unavailable(f"could not parse compose project name: {exc}")


def node_image() -> str:
    """The image compose built for ``node-a`` (``<project>-node-a`` by convention)."""
    return f"{_project_name()}-node-a"


def compose_network() -> str:
    """The default network compose created (``<project>_default``)."""
    return f"{_project_name()}_default"


def base_node_env() -> dict[str, str]:
    """Minimal env to boot a node, mirroring the compose ``node-a`` essentials.

    Deliberately omits ``ROUTSTR_SECRET_KEY``, ``ADMIN_PASSWORD``, ``NSEC`` and
    ``NPUB`` — each test layers in exactly the secret env it is exercising (and
    proves the *absence* of one by leaving it out). ``DATABASE_URL`` points at the
    mounted ``/data`` volume so state persists across a paired reboot.
    """
    return {
        "DATABASE_URL": "sqlite+aiosqlite:////data/node.db",
        "RELAYS": "ws://relay:8080",
        "UPSTREAM_BASE_URL": "http://mock-openai:3000",
        "UPSTREAM_API_KEY": "test-key",
        "CASHU_MINTS": "http://primary-mint:3338",
        "NAME": "LifecycleNode",
        "DESCRIPTION": "secret-lifecycle e2e node",
        "HTTP_URL": "http://localhost:8000",
        "CORS_ORIGINS": "*",
        "FIXED_COST_PER_REQUEST": "1",
        "FIXED_PER_1K_INPUT_TOKENS": "10",
        "FIXED_PER_1K_OUTPUT_TOKENS": "30",
        "FIXED_PRICING": "false",
    }


@dataclass
class BootResult:
    """Outcome of booting a node until it either exits or comes up serving."""

    exited: bool
    exit_code: int | None
    logs: str


@dataclass
class ServingNode:
    cid: str
    base_url: str

    def logs(self) -> str:
        return _logs(self.cid)


def _run_args(
    env: dict[str, str], *, volume: str, host_port: int | None
) -> list[str]:
    args = ["docker", "run", "-d", "--network", compose_network(), "-v", f"{volume}:/data"]
    if host_port is not None:
        args += ["-p", f"{host_port}:8000"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args.append(node_image())
    return args


def _docker_run(env: dict[str, str], *, volume: str, host_port: int | None) -> str:
    proc = subprocess.run(
        _run_args(env, volume=volume, host_port=host_port),
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        unavailable(
            f"failed to start ephemeral node: {(proc.stderr or proc.stdout)[:300]}"
        )
    return proc.stdout.strip()


def _logs(cid: str) -> str:
    proc = subprocess.run(["docker", "logs", cid], capture_output=True, text=True)
    return proc.stdout + proc.stderr


def _state(cid: str) -> tuple[str, int]:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", cid],
        capture_output=True,
        text=True,
    )
    status, _, code = proc.stdout.strip().partition(" ")
    return status or "unknown", int(code) if code.strip() else 0


def _rm(cid: str) -> None:
    subprocess.run(["docker", "rm", "-f", cid], capture_output=True, text=True)


@contextmanager
def throwaway_volume() -> Iterator[str]:
    """A named docker volume, removed on exit. Survives container removal so a
    paired reboot sees the first boot's on-disk state."""
    name = f"routstr-lifecycle-{uuid.uuid4().hex[:12]}"
    subprocess.run(["docker", "volume", "create", name], capture_output=True, text=True)
    try:
        yield name
    finally:
        subprocess.run(
            ["docker", "volume", "rm", "-f", name], capture_output=True, text=True
        )


def boot_until_settled(
    env: dict[str, str], *, volume: str, timeout: int = 60
) -> BootResult:
    """Boot a node and report whether it fails fast (exits) or stays up.

    Returns as soon as the container exits (``exited=True`` with its exit code).
    If it is still running after ``timeout`` it booted successfully
    (``exited=False``) — that is the signal a fail-fast did *not* happen. Either
    way the container is removed; the volume is the caller's to manage.
    """
    cid = _docker_run(env, volume=volume, host_port=None)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, code = _state(cid)
            if status == "exited":
                return BootResult(exited=True, exit_code=code, logs=_logs(cid))
            time.sleep(1)
        return BootResult(exited=False, exit_code=None, logs=_logs(cid))
    finally:
        _rm(cid)


@contextmanager
def serving_node(
    env: dict[str, str], *, volume: str, host_port: int = LIFECYCLE_PORT, timeout: int = 90
) -> Iterator[ServingNode]:
    """Boot a node, wait until it serves ``/v1/info``, yield a handle, then tear it
    down. Fails (or skips, ad hoc) if it exits during boot or never comes up."""
    cid = _docker_run(env, volume=volume, host_port=host_port)
    base = f"http://localhost:{host_port}"
    try:
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            status, code = _state(cid)
            if status == "exited":
                unavailable(
                    f"ephemeral node exited during boot (code {code}):\n"
                    f"{_logs(cid)[-1000:]}"
                )
            try:
                resp = httpx.get(f"{base}/v1/info", timeout=3)
                if resp.status_code < 500:
                    yield ServingNode(cid=cid, base_url=base)
                    return
            except httpx.HTTPError as exc:
                last_err = str(exc)
            time.sleep(1)
        unavailable(
            f"ephemeral node never became reachable on :{host_port} ({last_err})\n"
            f"{_logs(cid)[-1000:]}"
        )
    finally:
        _rm(cid)


def admin_login(base_url: str, password: str) -> str | None:
    """Mint an admin token from an ephemeral node, or None if login fails."""
    try:
        with httpx.Client(base_url=base_url, timeout=10) as client:
            resp = client.post("/admin/api/login", json={"password": password})
            return resp.json().get("token") if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
