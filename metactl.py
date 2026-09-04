"""Deployment action-catalog entrypoint for the generic CLI runtime.

This module is only composition.  All eVOLVER reads and mutations are sent
through the explicit trusted central transport; no edge persistence is
available from this server/operator CLI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

REPOSITORY_ROOT = Path(os.environ.get("META_WEBUI_REPOSITORY_ROOT", Path(__file__).resolve().parents[1]))

from framework.action_catalog import ActionCatalogError, load_action_catalog
from meta_webui_ui_runtime_textual.cli import run_cli
from tools.metactl_transport import ROUTE_BINDINGS, TransportError, configured_transport


def _catalog_paths(index_path: Path) -> list[Path]:
    """Resolve the deployment-owned, explicit catalog references."""
    import json

    document = json.loads(index_path.read_text(encoding="utf-8"))
    references = document.get("catalogs")
    if not isinstance(references, list):
        raise ActionCatalogError(f"{index_path}: catalogs must be a list of explicit references")
    expected = document.get("deployment_index")
    actual = [item.get("id") for item in references if isinstance(item, Mapping)]
    if actual != expected:
        raise ActionCatalogError(f"{index_path}: catalog references do not match deployment_index")
    paths: list[Path] = []
    for item in references:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str) or not isinstance(item.get("path"), str):
            raise ActionCatalogError(f"{index_path}: each catalog reference requires id and path")
        path = (index_path.parent / item["path"]).resolve()
        applications_root = index_path.parent.parent
        if applications_root not in path.parents:
            raise ActionCatalogError(f"{index_path}: catalog path escapes applications directory: {item['path']}")
        paths.append(path)
    return paths


def _layout(index_path: Path) -> dict[str, Any]:
    actions: dict[str, Any] = {}
    for path in _catalog_paths(index_path):
        catalog = load_action_catalog(path)
        for action in catalog.actions:
            identifier = action["id"]
            if identifier in actions:
                raise ActionCatalogError(f"duplicate deployment action id: {identifier}")
            entry = dict(action)
            entry["description"] = action["title"]
            entry["available"] = action["status"] == "implemented"
            entry["planned"] = action["status"] != "implemented"
            parameters = {}
            for name, spec in action.get("parameters", {}).items():
                parameter = dict(spec)
                if "enum" in parameter:
                    parameter["choices"] = parameter.pop("enum")
                parameters[name] = parameter
            entry["parameters"] = parameters
            actions[identifier] = entry
    return {"name": "metactl", "description": "Meta WebUI action catalog", "actions": actions}


def _redact(value: Any) -> Any:
    sensitive = ("credential", "password", "secret", "token", "private_key", "api_key", "authorization")
    if isinstance(value, dict):
        return {key: ("<redacted>" if any(part in str(key).lower() for part in sensitive) else _redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _registry(transport: Any) -> dict[str, Any]:
    def enrollment_output(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: (item if key == "enrollment_token" else "<redacted>" if key == "credential" else enrollment_output(item))
                    for key, item in value.items()}
        if isinstance(value, list):
            return [enrollment_output(item) for item in value]
        return value

    def invoke(parameters: Mapping[str, Any], action_id: str) -> Any:
        try:
            result = transport.action(action_id, parameters)
            # A one-time enrollment credential is the bounded output of this
            # explicit operator action; every other central projection stays
            # structurally redacted.  The token is never persisted by metactl.
            return enrollment_output(result) if action_id == "evolver.controllers.add" else _redact(result)
        except TransportError as error:
            return error.as_dict()
    return {action_id: (lambda params, action_id=action_id: invoke(params, action_id))
            for action_id in ROUTE_BINDINGS}


_HUMAN_ALIASES = {
    ("status",): "evolver.edge.status",
    ("controllers", "list"): "evolver.controllers.list",
    ("controllers", "show"): "evolver.controllers.show",
    ("controllers", "freshness"): "evolver.controllers.freshness",
    ("controllers", "add"): "evolver.controllers.add",
    ("controllers", "adopt"): "evolver.controllers.add",
    ("controllers", "refresh"): "evolver.controllers.refresh",
    ("controllers", "rescan"): "evolver.controllers.rescan",
    ("controllers", "commands", "list"): "evolver.controllers.commands.list",
    ("controllers", "commands", "show"): "evolver.controllers.commands.show",
    ("controllers", "recovery", "request"): "evolver.controllers.recovery.request",
    ("controllers", "recovery", "status"): "evolver.controllers.recovery.status",
    ("controllers", "recovery", "diff"): "evolver.controllers.recovery.diff",
    ("controllers", "release", "set"): "evolver.controllers.release.set",
    ("instruments", "list"): "evolver.instruments.list",
    ("instruments", "show"): "evolver.instruments.show",
    ("runs", "list"): "evolver.runs.list",
    ("runs", "show"): "evolver.runs.show",
    ("runs", "pause"): "evolver.runs.pause",
    ("runs", "resume"): "evolver.runs.resume",
    ("runs", "stop"): "evolver.runs.stop",
    ("releases", "build"): "evolver.release.build",
}


def _human_arguments(arguments: list[str]) -> list[str]:
    """Translate grouped operator syntax into stable action-id arguments."""
    leading = []
    while arguments and arguments[0] in {"--json", "--dry-run", "--yes"}:
        leading.append(arguments.pop(0))
    for prefix, action_id in sorted(_HUMAN_ALIASES.items(), key=lambda item: -len(item[0])):
        if tuple(arguments[:len(prefix)]) != prefix:
            continue
        tail = arguments[len(prefix):]
        if prefix == ("controllers", "adopt"):
            purpose_positions = [i for i, value in enumerate(tail) if value == "--purpose"]
            if purpose_positions and (purpose_positions[0] + 1 >= len(tail) or tail[purpose_positions[0] + 1] != "forced_adoption"):
                raise ValueError("controllers adopt requires --purpose forced_adoption")
            if not purpose_positions:
                tail.extend(["--purpose", "forced_adoption"])

        positional: list[str] = []
        if tail and not tail[0].startswith("-"):
            for value in tail:
                if value.startswith("-"):
                    break
                positional.append(value)
        if positional:
            if action_id == "evolver.controllers.add":
                option_names = ["--server-url"]
            elif action_id == "evolver.controllers.commands.show":
                option_names = ["--controller-id", "--command-id"]
            elif action_id == "evolver.controllers.release.set":
                option_names = ["--controller-id", "--release"]
            elif action_id == "evolver.instruments.show":
                option_names = ["--instrument-id"]
            elif action_id.startswith("evolver.runs."):
                option_names = ["--run-id"]
            else:
                option_names = ["--controller-id"]
            if len(positional) > len(option_names):
                raise ValueError(f"too many positional arguments for {' '.join(prefix)}")
            tail = [part for value, option in zip(positional, option_names) for part in (option, value)] + tail[len(positional):]
        return [*leading, action_id, *tail]
    return [*leading, *arguments]


_TERMINAL_DISPOSITIONS = frozenset({
    "duplicate", "rejected_stale_generation", "rejected_stale_revision",
    "rejected_unsafe", "rejected_invalid", "completed", "stored", "failed",
    "expired", "safe_stop_intent_recorded", "deferred_no_hardware_service",
    "rejected_lease", "quarantined",
})


def _watch(arguments: list[str], transport: Any, *, as_json: bool) -> int | None:
    prefix = ("controllers", "commands", "watch")
    leading = 0
    while leading < len(arguments) and arguments[leading] in {"--json", "--dry-run", "--yes"}:
        leading += 1
    if tuple(arguments[leading:leading + len(prefix)]) != prefix:
        return None
    tail = arguments[leading + len(prefix):]
    positionals: list[str] = []
    if tail and not tail[0].startswith("-"):
        for item in tail:
            if item.startswith("-"):
                break
            positionals.append(item)
    if len(positionals) != 2:
        raise ValueError("controllers commands watch requires <controller> <command>")
    controller_id, command_id = positionals
    def _option(name: str, default: float) -> float:
        if name not in tail:
            return default
        index = tail.index(name)
        if index + 1 >= len(tail):
            raise ValueError(f"{name} requires a value")
        return float(tail[index + 1])
    timeout = _option("--timeout", 30.0)
    interval = _option("--interval", 1.0)
    if timeout < 0 or interval <= 0 or timeout > 300 or interval > 60:
        raise ValueError("watch timeout must be 0..300 and interval must be >0..60")
    deadline = time.monotonic() + timeout
    result: Any = None
    while True:
        try:
            result = _redact(transport.action("evolver.controllers.commands.show", {
                "controller_id": controller_id, "command_id": command_id}))
        except TransportError as error:
            result = error.as_dict()
            disposition = None
            break
        command = result.get("command", result) if isinstance(result, Mapping) else {}
        disposition = command.get("disposition") if isinstance(command, Mapping) else None
        if disposition in _TERMINAL_DISPOSITIONS:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    terminal = disposition in _TERMINAL_DISPOSITIONS
    payload = {"status": "completed" if terminal else "timeout", "action": "evolver.controllers.commands.show",
               "result": {"controller_id": controller_id, "command_id": command_id,
                           "disposition": disposition, "command": result,
                           "accepted_or_queued": disposition in {"accepted", "queued"},
                           "physical_actuation_verified": bool(
                               isinstance(command, Mapping) and command.get("physical_actuation_verified") is True)}}
    print(json.dumps(payload, sort_keys=True, default=str))
    return 0


def main(argv: list[str] | None = None, *, transport: Any | None = None) -> int:
    index_path = REPOSITORY_ROOT / "applications" / "deployment" / "action-catalog.json"
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        # Human-facing grouped aliases remain presentation-only; the action ID
        # is the stable contract and still drives the same explicit binding.
        chosen_transport = transport or configured_transport()
        watch_result = _watch(arguments, chosen_transport, as_json="--json" in arguments)
        if watch_result is not None:
            return watch_result
        return run_cli(_layout(index_path), _registry(chosen_transport), _human_arguments(arguments))
    except (ActionCatalogError, OSError, ValueError, KeyError) as error:
        print(f"metactl: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
