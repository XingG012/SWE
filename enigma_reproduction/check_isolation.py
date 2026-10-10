#!/usr/bin/env python3
"""Two real CTF services, same DNS/port, zero model calls and no flag submission."""
from __future__ import annotations

import json
import multiprocessing
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sweagent.environment.ctf_isolation import TaskIsolation
from sweagent.environment.utils import attach_network_interface_to_container, get_docker_compose
from sweagent.utils.secrets import redact


def worker(connection, directory, challenge, image, expected):
    task = TaskIsolation(Path(directory), Path(challenge), ROOT / "enigma_reproduction/generated/ctf-execution.lock")
    solver = None
    try:
        with task:
            get_docker_compose(Path(challenge).parent / "docker-compose.yml")
            solver = task.client.containers.run(image, ["sleep", "300"], detach=True,
                labels={"enigma.project": task.project}, name=f"{task.project}-solver")
            attach_network_interface_to_container(solver.name)

            def probe():
                code = ("import socket,json; s=socket.create_connection(('rev.chal.csaw.io',8000),timeout=10); "
                        "print(json.dumps({'banner':s.recv(128).decode().strip(),'peer':s.getpeername()[0]})); s.close()")
                result = solver.exec_run(["/root/miniconda3/bin/python", "-c", code])
                if result.exit_code:
                    raise RuntimeError("Solver service probe failed")
                result = json.loads(result.output)
                if result["banner"] != expected:
                    raise RuntimeError(f"Wrong service banner: {result['banner']}")
                return {**result, "project": task.project, "network": task.network}

            connection.send({"ready": True, **probe()})
            while True:
                command = connection.recv()
                if command == "stop":
                    break
                if command == "probe":
                    connection.send(probe())
        connection.send({"stopped": True, "cleanup_errors": task.cleanup_errors})
    except BaseException as error:
        connection.send({"error": redact(f"{type(error).__name__}: {error}")})
    finally:
        task.close()
        connection.close()


def receive(connection):
    if not connection.poll(55):
        raise RuntimeError("Isolation smoke worker did not respond within 55 seconds")
    message = connection.recv()
    if "error" in message:
        raise RuntimeError(message["error"])
    return message


def main():
    import docker
    with closing(docker.from_env(timeout=10)) as client:
        image = client.images.get("sweagent/enigma").id
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = ROOT / "enigma_reproduction/generated" / f"isolation-smoke-{stamp}"
    directory.mkdir(mode=0o700, parents=True)
    dataset = Path("/home/xing/.nyuctf/v20250206/test")
    cases = [("sfc", dataset / "2021/CSAW-Finals/rev/sfc/challenge.json", "WELCOME"),
             ("gibberish", dataset / "2019/CSAW-Quals/rev/gibberish_check/challenge.json", "Find the Key!")]
    context = multiprocessing.get_context("spawn")
    children = []
    try:
        for name, challenge, expected in cases:
            parent, child = context.Pipe()
            process = context.Process(target=worker,
                args=(child, str(directory / name), str(challenge), image, expected))
            process.start()
            child.close()
            children.append((parent, process))
        initial = [receive(c) for c, p in children]
        assert initial[0]["network"] != initial[1]["network"]
        children[0][0].send("stop")
        stopped_a = receive(children[0][0])
        children[0][1].join(5)
        children[1][0].send("probe")
        after_cleanup = receive(children[1][0])
        children[1][0].send("stop")
        stopped_b = receive(children[1][0])
        children[1][1].join(5)
        assert not stopped_a["cleanup_errors"] and not stopped_b["cleanup_errors"]
        with closing(docker.from_env(timeout=10)) as client:
            for record in initial:
                assert not client.containers.list(all=True, filters={"label": f"com.docker.compose.project={record['project']}"})
                assert not client.containers.list(all=True, filters={"label": f"enigma.project={record['project']}"})
                assert not client.networks.list(names=[record["network"]])
        report = {"ok": True, "model_calls": 0, "simultaneous_services": initial,
                  "second_service_after_first_cleanup": after_cleanup, "cleanup_verified": True}
        (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({**report, "report": str(directory / "report.json")}, ensure_ascii=False))
    finally:
        for connection, process in children:
            if process.is_alive():
                try:
                    connection.send("stop")
                    process.join(10)
                except (BrokenPipeError, EOFError):
                    pass
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            connection.close()


if __name__ == "__main__":
    main()
