"""Per-attempt CTF service isolation without changing challenge files or domains."""
from __future__ import annotations

import copy
from contextlib import closing
import fcntl
import json
import os
import subprocess
import uuid
from pathlib import Path

import docker
import yaml

from sweagent.utils.secrets import credential_free_environment, redact

ISOLATION_VERSION = "per_task_v1"


def isolated_config(config: dict, project: str, network: str, *, allow_host=False) -> dict:
    """Transform Docker's resolved config, retaining paths, aliases and topology."""
    result = copy.deepcopy(config)
    result["name"] = project
    networks = result.setdefault("networks", {})
    networks["ctfnet"] = {"external": True, "name": network}
    for key, spec in networks.items():
        if key == "ctfnet":
            continue
        if spec.get("external"):
            raise RuntimeError(f"Unsupported external challenge network: {key}")
        spec["name"] = f"{project}_{key}"
    for key, spec in result.get("volumes", {}).items():
        if spec.get("external"):
            raise RuntimeError(f"Unsupported external challenge volume: {key}")
        spec["name"] = f"{project}_{key}"
    for service in result.get("services", {}).values():
        if service.get("network_mode") == "host" and not allow_host:
            raise RuntimeError("Host-network challenge requires exclusive execution")
        service.pop("container_name", None)
        service.pop("ports", None)  # Solver reaches internal ports on its own network.
    return result


def prepare_compose(source: Path) -> Path:
    """Resolve relative mounts/build paths before writing a private runtime copy."""
    project = os.environ["ENIGMA_CTF_PROJECT"]
    rendered = subprocess.run(
        ["docker", "compose", "-p", project, "-f", str(source), "config", "--format", "json"],
        env=credential_free_environment(), capture_output=True, text=True, timeout=60,
    )
    if rendered.returncode:
        raise RuntimeError(f"Docker Compose configuration failed: {redact(rendered.stderr)[-2000:]}")
    config = isolated_config(json.loads(rendered.stdout), project, os.environ["ENIGMA_CTF_NETWORK"],
                             allow_host=os.environ.get("ENIGMA_CTF_ALLOW_HOST") == "1")
    path = Path(os.environ["ENIGMA_CTF_DIRECTORY"]) / "compose.json"
    with os.fdopen(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as output:
        json.dump(config, output)
    os.environ["ENIGMA_CTF_COMPOSE_FILE"] = str(path)
    return path


def verify_network_members(network: str, project: str) -> None:
    """A private network must contain only services from this attempt's project."""
    with closing(docker.from_env(timeout=10)) as client:
        info = client.networks.get(network).attrs
        for identifier in info.get("Containers", {}):
            labels = client.containers.get(identifier).attrs["Config"].get("Labels") or {}
            if labels.get("com.docker.compose.project") != project:
                raise RuntimeError("CTF network contains a container from another attempt")


def verify_solver_dns(container: str) -> None:
    """Check that the challenge hostname resolves to this task's service IPs."""
    host = os.environ.get("ENIGMA_CTF_BOX")
    if not host or not os.environ.get("ENIGMA_CTF_COMPOSE_FILE"):
        return
    network = os.environ["ENIGMA_CTF_NETWORK"]
    allowed = set()
    with closing(docker.from_env(timeout=10)) as client:
        for identifier in client.networks.get(network).attrs.get("Containers", {}):
            info = client.containers.get(identifier).attrs
            connection = info["NetworkSettings"]["Networks"][network]
            if host in (connection.get("Aliases") or []) + (connection.get("DNSNames") or []):
                labels = info["Config"].get("Labels") or {}
                if labels.get("com.docker.compose.project") != os.environ["ENIGMA_CTF_PROJECT"]:
                    raise RuntimeError("Challenge hostname belongs to a different Compose project")
                allowed.add(connection["IPAddress"])
    if not allowed:
        raise RuntimeError(f"Challenge hostname {host} is missing from this attempt's network")
    script = "import json,socket,sys; print(json.dumps(sorted({r[4][0] for r in socket.getaddrinfo(sys.argv[1],None,type=socket.SOCK_STREAM)})))"
    result = subprocess.run(["docker", "exec", container, "/root/miniconda3/bin/python", "-c", script, host],
                            env=credential_free_environment(), capture_output=True, text=True, timeout=30)
    resolved = set(json.loads(result.stdout)) if result.returncode == 0 else set()
    if not resolved or not resolved.issubset(allowed):
        raise RuntimeError("Solver DNS does not resolve exclusively to this attempt's services")


class TaskIsolation:
    """Use a unique network/project and clean up only this attempt's resources.

    The one NYU challenge with a host-network sidecar takes an exclusive lock;
    ordinary attempts share the lock and can run concurrently across models.
    """
    def __init__(self, directory: Path, challenge_path: Path, lock_path: Path):
        self.directory = directory
        self.project = f"enigma-{uuid.uuid4().hex}"
        self.network = f"{self.project}-ctfnet"
        self.lock_path = lock_path
        self.client = None
        self.network_obj = None
        self.lock = None
        self.previous = {}
        self.cleanup_errors = []
        challenge = json.loads(challenge_path.read_text())
        self.box = challenge.get("box") or ""
        compose = challenge_path.parent / "docker-compose.yml"
        config = (yaml.safe_load(compose.read_text()) or {}) if challenge.get("compose") and compose.exists() else {}
        self.exclusive = any(s.get("network_mode") == "host" for s in config.get("services", {}).values())

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.lock_path.open("a")
        fcntl.flock(self.lock, fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH)
        try:
            self.client = docker.from_env(timeout=10)
            self.network_obj = self.client.networks.create(
                self.network, driver="bridge", labels={"enigma.isolation": ISOLATION_VERSION,
                                                       "enigma.project": self.project})
            values = {"ENIGMA_CTF_NETWORK": self.network, "ENIGMA_CTF_PROJECT": self.project,
                      "ENIGMA_CTF_DIRECTORY": str(self.directory),
                      "ENIGMA_CTF_ALLOW_HOST": "1" if self.exclusive else "0",
                      "ENIGMA_CTF_BOX": self.box,
                      "ENIGMA_CTF_COMPOSE_FILE": ""}
            self.previous = {key: os.environ.get(key) for key in values}
            os.environ.update(values)
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.client is not None:
            # Also catches partial Compose startup / Main initialization failures.
            try:
                compose = self.directory / "compose.json"
                if compose.exists():
                    from sweagent.environment.utils import terminate_docker_compose
                    terminate_docker_compose(compose)
            except Exception as error:
                self.cleanup_errors.append(redact(f"{type(error).__name__}: {error}"))
            try:
                for label in (f"com.docker.compose.project={self.project}", f"enigma.project={self.project}"):
                    for container in self.client.containers.list(all=True, filters={"label": label}):
                        try:
                            container.remove(force=True)
                        except Exception as error:
                            self.cleanup_errors.append(redact(f"{type(error).__name__}: {error}"))
                networks = self.client.networks.list(filters={"label": f"com.docker.compose.project={self.project}"})
                if self.network_obj is not None:
                    networks.append(self.network_obj)
                for network in networks:
                    try:
                        network.remove()
                    except Exception as error:
                        self.cleanup_errors.append(redact(f"{type(error).__name__}: {error}"))
            except Exception as error:
                self.cleanup_errors.append(redact(f"{type(error).__name__}: {error}"))
            finally:
                self.client.close()
                self.client = None
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.previous.clear()
        if self.lock is not None:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None

    def __exit__(self, *unused):
        self.close()
