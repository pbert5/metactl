"""Deployment action-catalog entrypoint for the generic CLI runtime.

This module is only composition.  All eVOLVER reads and mutations are sent
through the explicit trusted central transport; no edge persistence is
available from this server/operator CLI.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REPOSITORY_ROOT = Path(os.environ.get("META_WEBUI_REPOSITORY_ROOT", Path(__file__).resolve().parents[1]))

try:
    from .framework.action_catalog import ActionCatalogError, load_action_catalog
    from .meta_webui_ui_runtime_textual.cli import build_parser, run_cli
    from .metactl_transport import TransportError, configured_transport, operator_action_ids
except ImportError:  # direct loading from the extracted checkout
    from framework.action_catalog import ActionCatalogError, load_action_catalog
    from meta_webui_ui_runtime_textual.cli import build_parser, run_cli
    from metactl_transport import TransportError, configured_transport, operator_action_ids


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
        applications_root = index_path.parent.parent.resolve()
        if applications_root not in path.parents:
            raise ActionCatalogError(f"{index_path}: catalog path escapes applications directory: {item['path']}")
        paths.append(path)
    return paths


def _layout_from_single_catalog(path: Path) -> dict[str, Any]:
    catalog = load_action_catalog(path)
    actions = {}
    for action in catalog.actions:
        entry = dict(action)
        entry["description"] = action["title"]
        entry["available"] = action["status"] == "implemented"
        entry["planned"] = action["status"] != "implemented"
        actions[action["id"]] = entry
    return {"name": "metactl", "description": "eVOLVER operator actions", "actions": actions}


def _layout(index_path: Path) -> dict[str, Any]:
    document = json.loads(index_path.read_text(encoding="utf-8"))
    if isinstance(document.get("actions"), list):
        return _layout_from_single_catalog(index_path)
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
                parameters[name] = parameter
            entry["parameters"] = parameters
            actions[identifier] = entry
    presentation_path = index_path.parent / "metactl-cli.json"
    presentation = json.loads(presentation_path.read_text(encoding="utf-8")) if presentation_path.is_file() else {}
    return {"name": "metactl", "description": "Meta WebUI action catalog", "actions": actions,
            "presentation": presentation}


def _redact(value: Any) -> Any:
    sensitive = ("credential", "password", "secret", "token", "private_key", "api_key", "authorization")
    if isinstance(value, dict):
        return {key: ("<redacted>" if any(part in str(key).lower() for part in sensitive) else _redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str) and value.startswith(("http://", "https://", "//")):
        return _safe_target_url(value, sensitive)
    return value


def _safe_target_url(value: str, sensitive: tuple[str, ...]) -> str:
    try:
        parts = urlsplit(value)
        if not parts.netloc or (not parts.scheme and not value.startswith("//")):
            return value
        hostname = parts.hostname or ""
        port = f":{parts.port}" if parts.port is not None else ""
        query = urlencode([
            (key, "<redacted>" if any(part in key.lower() for part in sensitive) else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, hostname + port, parts.path, query, ""))
    except ValueError:
        return "<redacted URL>"


def _present_command_result(value: Any) -> Any:
    if not isinstance(value, Mapping) or not isinstance(value.get("command"), Mapping):
        return value
    command = value["command"]
    disposition = command.get("disposition")
    physical = command.get("physical_actuation_verified")
    if not isinstance(physical, bool):
        physical = None
    return {
        **value,
        "disposition": disposition,
        "accepted_or_queued": disposition in {"accepted", "queued"},
        "accepted": disposition == "accepted",
        "queued": disposition == "queued",
        "physical_actuation_verified": physical,
    }


def _registry(transport: Any) -> dict[str, Any]:
    def invoke(parameters: Mapping[str, Any], action_id: str) -> Any:
        try:
            result = transport.action(action_id, parameters)
            return _present_command_result(_redact(result))
        except TransportError as error:
            return error.as_dict()
    return {action_id: (lambda params, action_id=action_id: invoke(params, action_id))
            for action_id in operator_action_ids()}


_HUMAN_ALIASES = {
    ("status",): "evolver.edge.status",
    ("control", "status"): "evolver.edge.status",
    ("control", "controllers"): "evolver.controllers.list",
    ("control", "instruments"): "evolver.instruments.list",
    ("control", "runs"): "evolver.runs.list",
    ("controllers", "list"): "evolver.controllers.list",
    ("controllers", "show"): "evolver.controllers.show",
    ("controllers", "freshness"): "evolver.controllers.freshness",
    ("controllers", "add"): "evolver.controllers.add",
    ("controllers", "adopt"): "evolver.controllers.add",
    ("controllers", "refresh"): "evolver.controllers.refresh",
    ("controllers", "rescan"): "evolver.controllers.rescan",
    ("controllers", "archive"): "evolver.controllers.archive",
    ("controllers", "restore"): "evolver.controllers.restore",
    ("controllers", "commands", "list"): "evolver.controllers.commands.list",
    ("controllers", "commands", "show"): "evolver.controllers.commands.show",
    ("controllers", "manual", "lease"): "evolver.controllers.manual.lease",
    ("controllers", "manual", "lease", "show"): "evolver.controllers.manual.lease.show",
    ("controllers", "manual", "lease", "revoke"): "evolver.controllers.manual.lease.revoke",
    ("controllers", "manual", "lease", "emergency-release"): "evolver.controllers.manual.lease.emergency_release",
    ("controllers", "manual", "command"): "evolver.controllers.manual.command",
    ("controllers", "manual", "stir"): "evolver.controllers.manual.stir",
    ("controllers", "measurements"): "evolver.controllers.measurements",
    ("controllers", "telemetry"): "evolver.controllers.telemetry",
    ("controllers", "activities"): "evolver.controllers.activities",
    ("controllers", "events"): "evolver.controllers.events",
    ("controllers", "evidence"): "evolver.controllers.evidence",
    ("controllers", "logs"): "evolver.controllers.logs",
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
    ("validation", "experiment"): "evolver.experiments.validate",
    ("experiments", "validation"): "evolver.experiments.validate",
    ("releases", "build"): "evolver.release.build",
}


def _interactive(arguments: list[str], index_path: Path, transport: Any, *, input_stream: Any = None,
                  output: Any = None) -> int | None:
    """Run a small catalog-driven prompt for operators at a real terminal."""
    if arguments != ["interactive"]:
        return None
    layout = _layout(index_path)
    actions = [entry for entry in layout["actions"].values() if entry["available"]]
    if not actions:
        raise ValueError("the action catalog has no available actions")
    input_stream = input_stream or sys.stdin
    output = output or sys.stdout
    output.write("metactl interactive\n")
    for number, action in enumerate(actions, 1):
        output.write(f"{number}) {action['title']} [{action['id']}]\n")
    output.write("Choose an action (q to quit): ")
    choice = input_stream.readline().strip()
    if choice.lower() in {"", "q", "quit", "exit"}:
        return 0
    try:
        action = actions[int(choice) - 1]
    except (ValueError, IndexError) as error:
        raise ValueError("interactive choice must be a listed action number") from error
    command = [action["id"]]
    for name, spec in action.get("parameters", {}).items():
        if "default" in spec:
            prompt = f"{name} [{spec['default']}]: "
        else:
            prompt = f"{name}{' (required)' if spec.get('required') else ''}: "
        output.write(prompt)
        value = input_stream.readline()
        value = value.strip()
        if not value and "default" in spec:
            value = str(spec["default"])
        if value:
            command.extend([f"--{name.replace('_', '-')}", value])
    return run_cli(layout, _registry(transport), command, input=input_stream, output=output)


def _human_arguments(arguments: list[str], presentation: Mapping[str, Any] | None = None) -> list[str]:
    """Translate grouped operator syntax into stable action-id arguments."""
    leading = []
    while arguments and arguments[0] in {"--json", "--dry-run", "--yes"}:
        leading.append(arguments.pop(0))
    aliases = dict(_HUMAN_ALIASES)
    defaults: dict[tuple[str, ...], Mapping[str, Any]] = {}
    def collect(tree: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> None:
        for name, value in tree.items():
            leaf = value if isinstance(value, str) else value.get("action_id") if isinstance(value, Mapping) else None
            if isinstance(leaf, str):
                aliases.setdefault(prefix + (name,), leaf)
                if isinstance(value, Mapping):
                    defaults[prefix + (name,)] = value.get("defaults", {})
            elif isinstance(value, Mapping):
                children = value.get("commands", value)
                if isinstance(children, Mapping):
                    collect(children, prefix + (name,))
    if isinstance(presentation, Mapping):
        groups = presentation.get("groups", presentation)
        if isinstance(groups, Mapping):
            collect(groups)
    for prefix, action_id in sorted(aliases.items(), key=lambda item: -len(item[0])):
        if tuple(arguments[:len(prefix)]) != prefix:
            continue
        tail = arguments[len(prefix):]
        if prefix == ("controllers", "adopt"):
            purpose_positions = [i for i, value in enumerate(tail) if value == "--purpose"]
            if purpose_positions and (purpose_positions[0] + 1 >= len(tail) or tail[purpose_positions[0] + 1] != "forced_adoption"):
                raise ValueError("controllers adopt requires --purpose forced_adoption")
            if not purpose_positions:
                tail.extend(["--purpose", "forced_adoption"])
        for name, value in defaults.get(prefix, {}).items():
            option = f"--{name.replace('_', '-')}"
            if option not in tail:
                tail.extend([option, str(value)])

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


def _watch(arguments: list[str], transport: Any, *, as_json: bool,
           output: Any = None) -> int | None:
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
    physical = command.get("physical_actuation_verified") if isinstance(command, Mapping) else None
    if not isinstance(physical, bool):
        physical = None
    payload = {"status": "completed" if terminal else "timeout", "action": "evolver.controllers.commands.show",
               "result": {"controller_id": controller_id, "command_id": command_id,
                           "disposition": disposition, "command": result,
                           "accepted_or_queued": disposition in {"accepted", "queued"},
                           "accepted": disposition == "accepted",
                           "queued": disposition == "queued",
                           "physical_actuation_verified": physical}}
    output = output or sys.stdout
    output.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
    return 0


def main(argv: list[str] | None = None, *, transport: Any | None = None,
         input: Any | None = None, output: Any | None = None) -> int:
    index_path = Path(__file__).with_name("applications") / "deployment" / "action-catalog.json"
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        # A bare invocation is a discovery landing page, not an incomplete
        # action request.  Build the normal parser so help stays authoritative
        # and does not instantiate a transport or contact central.
        if not arguments:
            parser = build_parser(_layout(index_path))
            output = output or sys.stdout
            output.write("Meta BAL operator CLI\n\n")
            output.write("Common discovery paths:\n")
            output.write("  metactl actions list\n")
            output.write("  metactl interactive\n")
            output.write("  metactl tui\n")
            output.write("  metactl api\n")
            output.write("  metactl api check --repo .\n")
            output.write("  metactl api tui --repo .\n\n")
            parser.print_help(output)
            return 0
        if arguments[:1] == ["api"]:
            try:
                from .api_workbench.cli import main as api_main
            except ImportError:
                from api_workbench.cli import main as api_main
            return api_main(arguments[1:], output=output)
        if arguments[:1] == ["tui"]:
            try:
                from .operator_tui.cli import main as operator_main
            except ImportError:
                from operator_tui.cli import main as operator_main
            return operator_main(arguments[1:], transport=transport, output=output)
        if arguments[:1] == ["doctor"]:
            parser = argparse.ArgumentParser(prog="metactl doctor",
                                             description="run read-only operator diagnostics")
            parser.add_argument("--format", choices=("json",), default="json")
            parser.parse_args(arguments[1:])
            try:
                from .doctor import doctor_report
            except ImportError:
                from doctor import doctor_report
            output = output or sys.stdout
            json.dump(doctor_report(transport=transport), output, sort_keys=True, default=str)
            output.write("\n")
            return 0
        # Human-facing grouped aliases remain presentation-only; the action ID
        # is the stable contract and still drives the same explicit binding.
        chosen_transport = transport or configured_transport()
        interactive_result = _interactive(arguments, index_path, chosen_transport,
                                          input_stream=input, output=output)
        if interactive_result is not None:
            return interactive_result
        watch_result = _watch(arguments, chosen_transport, as_json="--json" in arguments,
                              output=output)
        if watch_result is not None:
            return watch_result
        layout = _layout(index_path)
        return run_cli(layout, _registry(chosen_transport), _human_arguments(arguments, layout.get("presentation")))
    except (ActionCatalogError, OSError, ValueError, KeyError) as error:
        print(f"metactl: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
