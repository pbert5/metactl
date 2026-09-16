"""Resolve local pytest node IDs without executing imported catalog strings."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import selectors
import signal
import subprocess
import threading
import time

from .model import WorkbenchError
from .execution import redact


@dataclass(frozen=True)
class TestTarget:
    root: Path
    node: str
    component: str | None


def resolve_tests(endpoint) -> list[TestTarget]:
    if endpoint.kind != "catalog" or endpoint.repository is None:
        raise WorkbenchError("only a local repository catalog can select executable tests")
    root = Path(endpoint.repository).resolve()
    umbrella = (root / "tools/test").is_file() and (root / "metactl").is_dir()
    roots = {"root": root, "metactl": root / "metactl", "server": root / "evolver/evolver-server",
             "controller": root / "evolver/evolver-controller", "hardware": root / "evolver/evolver-hardware"} if umbrella else {None: root}
    targets = []
    refs = endpoint.evidence.get("tests", [])
    if not refs:
        raise WorkbenchError("no evidence tests declared")
    for ref in refs:
        parts = ref.split("::")
        filename = Path(parts[0])
        if filename.is_absolute() or ".." in filename.parts or not filename.as_posix().endswith(".py") or not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", p) for p in parts[1:]):
            raise WorkbenchError("evidence must be a relative Python test file with optional class/function node IDs")
        candidates = []
        for component, component_root in roots.items():
            target = (component_root / filename).resolve()
            if target.is_relative_to(component_root.resolve()) and target.is_file():
                candidates.append(TestTarget(root if umbrella else component_root, str(target.relative_to(root if umbrella else component_root)) + "".join("::" + p for p in parts[1:]), component))
        # A fully qualified umbrella-relative reference is accepted only once.
        if len(candidates) > 1:
            raise WorkbenchError(f"ambiguous evidence test file: {ref}")
        if not candidates:
            raise WorkbenchError(f"evidence test not present in this checkout: {ref}")
        targets.append(candidates[0])
    return targets


def command_for(target: TestTarget) -> list[str]:
    if target.component is not None:
        return ["rtk", "tools/test", "evidence", target.component, target.node]
    return ["rtk", "uv", "run", "--project", str(target.root), "pytest", "-q", target.node]


def run_tests(targets, *, trusted=False, emit=lambda text: None, cancel=None, timeout=300):
    if not trusted:
        raise WorkbenchError("executing local pytest evidence requires explicit trust confirmation")
    cancel = cancel or threading.Event()
    results = []
    for target in dict.fromkeys(targets):
        if cancel.is_set():
            break
        # Evidence runs use the supported container environment and inherit its
        # repository dependencies, but do not inherit operator HTTP credentials.
        env = {k: v for k, v in os.environ.items() if not k.startswith("META_WEBUI_METACTL_") and k != "META_WEBUI_EVOLVER_CONTROL_SHARED_SECRET"}
        command = command_for(target)
        emit(f"Running {target.node}\n")
        try:
            process = subprocess.Popen(command, cwd=target.root, env=env, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise WorkbenchError("evidence runner unavailable; use the Meta BAL Server Dev Container with RTK") from exc
        deadline = time.monotonic() + timeout
        captured, stopped = 0, False
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if cancel.is_set() or time.monotonic() >= deadline:
                    os.killpg(process.pid, signal.SIGTERM)
                    stopped = True
                    break
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    captured += len(chunk)
                    # Do not retain logs in request history. The explicit local
                    # test console is a live developer view with a bounded tail.
                    if captured <= 1024 * 1024:
                        emit(chunk.decode("utf-8", errors="replace"))
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                code = process.wait()
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        results.append({"node": target.node, "exit_code": code, "passed": code == 0 and not stopped,
                        "stopped": stopped, "output_truncated": captured > 1024 * 1024})
    return results
