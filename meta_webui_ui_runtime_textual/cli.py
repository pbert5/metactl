"""Generic declarative command-line runtime for the terminal shell.

The runtime knows how to present and dispatch configured actions.  It does not
know what an application action means; the injected registry is the only
dispatch authority.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO


Json = dict[str, Any]
ActionHandler = Callable[[Mapping[str, Any]], Any]


class CLIError(ValueError):
    """A malformed layout or an invalid CLI request."""


@dataclass(frozen=True)
class Action:
    name: str
    description: str
    parameters: dict[str, Json]
    available: bool = True
    confirmation: str | None = None
    planned: bool = False
    registry_binding: str | None = None
    status: str = "implemented"
    tags: tuple[str, ...] = ()


def load_layout(value: Mapping[str, Any] | str) -> Mapping[str, Any]:
    """Load a JSON layout mapping from a mapping or a JSON string/path."""
    if isinstance(value, Mapping):
        return value
    try:
        with open(value, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, TypeError):
        try:
            loaded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise CLIError("CLI layout must be a mapping, JSON string, or file") from exc
    if not isinstance(loaded, Mapping):
        raise CLIError("CLI layout JSON must contain an object")
    return loaded


def _merged_action(raw: Mapping[str, Any], actions: Mapping[str, Any], name: str,
                   seen: set[str] | None = None) -> Json:
    seen = set() if seen is None else seen
    if name in seen:
        raise CLIError(f"cyclic action inheritance involving {name}")
    seen.add(name)
    parent_name = raw.get("extends", raw.get("inherits"))
    parent: Json = {}
    if isinstance(parent_name, str):
        parent_raw = actions.get(parent_name)
        if not isinstance(parent_raw, Mapping):
            raise CLIError(f"unknown parent action: {parent_name}")
        parent = _merged_action(parent_raw, actions, parent_name, seen)
    merged = dict(parent)
    merged.update({key: value for key, value in raw.items() if key not in {"parameters", "extends", "inherits"}})
    parameters = dict(parent.get("parameters", {}))
    child_parameters = raw.get("parameters", {})
    if isinstance(child_parameters, Mapping):
        parameters.update({str(key): dict(spec) if isinstance(spec, Mapping) else {"default": spec}
                           for key, spec in child_parameters.items()})
    elif child_parameters:
        raise CLIError(f"parameters for {name} must be an object")
    merged["parameters"] = parameters
    return merged


def actions_from_layout(layout: Mapping[str, Any]) -> dict[str, Action]:
    raw_actions = layout.get("actions", {})
    if isinstance(raw_actions, list):
        raw_actions = {item.get("id"): item for item in raw_actions
                       if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
    if not isinstance(raw_actions, Mapping):
        raise CLIError("layout actions must be an object or list")
    defaults = layout.get("action_defaults", {})
    if not isinstance(defaults, Mapping):
        raise CLIError("action_defaults must be an object")
    result: dict[str, Action] = {}
    for name, raw in raw_actions.items():
        if not isinstance(raw, Mapping):
            raise CLIError(f"action {name} must be an object")
        merged = dict(defaults)
        merged.update(_merged_action(raw, raw_actions, str(name)))
        parameters = merged.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise CLIError(f"parameters for {name} must be an object")
        safety = merged.get("safety", {})
        safety_confirmation = safety.get("confirmation") if isinstance(safety, Mapping) else None
        status = str(merged.get("status", "implemented"))
        result[str(name)] = Action(
            name=str(name), description=str(merged.get("description", merged.get("title", ""))),
            parameters={str(k): dict(v) for k, v in parameters.items() if isinstance(v, Mapping)},
            available=bool(merged.get("available", status == "implemented")),
            confirmation=str(merged.get("confirmation", "Confirm action?")) if merged.get("confirmation", safety_confirmation) not in (None, "none") else None,
            planned=bool(merged.get("planned", status != "implemented")),
            registry_binding=(str(merged["registry"]["binding"])
                              if isinstance(merged.get("registry"), Mapping) and merged["registry"].get("binding") else None),
            status=status, tags=tuple(str(tag) for tag in merged.get("tags", [])),
        )
    return result


def _type(spec: Mapping[str, Any]) -> Callable[[str], Any]:
    kind = spec.get("type", "string")
    if kind in ("string", "str"): return str
    if kind in ("integer", "int"): return int
    if kind in ("number", "float"): return float
    if kind in ("boolean", "bool"): return lambda value: value.lower() in {"1", "true", "yes", "on"}
    if kind in ("json", "object", "array"): return json.loads
    raise CLIError(f"unsupported parameter type: {kind}")


def _parameter_args(parser: argparse.ArgumentParser, action: Action) -> None:
    for name, spec in action.parameters.items():
        option = str(spec.get("option", f"--{name.replace('_', '-')}"))
        kwargs: Json = {"dest": name, "help": spec.get("description", ""), "type": _type(spec)}
        if "default" in spec: kwargs["default"] = spec["default"]
        kwargs["required"] = bool(spec.get("required", False))
        if isinstance(spec.get("choices"), list): kwargs["choices"] = spec["choices"]
        parser.add_argument(option, **kwargs)


def _shell_args(parser: argparse.ArgumentParser) -> None:
    """Allow shell switches after a subcommand as well as before it."""
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit machine-readable output")
    parser.add_argument("--dry-run", action="store_true", default=argparse.SUPPRESS,
                        help="show the planned dispatch without executing it")
    parser.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                        help="confirm actions requiring confirmation")


def build_parser(layout: Mapping[str, Any], actions: Mapping[str, Action] | None = None) -> argparse.ArgumentParser:
    actions = actions or actions_from_layout(layout)
    parser = argparse.ArgumentParser(prog=str(layout.get("name", "meta-webui")), description=layout.get("description"))
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    parser.add_argument("--dry-run", action="store_true", help="show the planned dispatch without executing it")
    parser.add_argument("--yes", action="store_true", help="confirm actions requiring confirmation")
    sub = parser.add_subparsers(dest="command", required=True)
    info = sub.add_parser("actions", help="inspect configured actions")
    _shell_args(info)
    info_sub = info.add_subparsers(dest="action_command", required=True)
    listing = info_sub.add_parser("list")
    _shell_args(listing)
    listing.add_argument("--filter", default="", help="filter by action name or description")
    listing.add_argument("--status", default=None, help="filter by lifecycle status")
    listing.add_argument("--tag", action="append", default=[], help="filter by action tag (repeatable)")
    listing.add_argument("--available", action="store_true", help="show available actions only")
    showing = info_sub.add_parser("show")
    _shell_args(showing)
    showing.add_argument("name", choices=sorted(actions))
    for name, action in actions.items():
        command = sub.add_parser(name, help=action.description)
        _shell_args(command)
        _parameter_args(command, action)
    return parser


def _emit(value: Any, *, as_json: bool, output: TextIO) -> None:
    if as_json:
        json.dump(value, output, sort_keys=True, default=str)
        output.write("\n")
    elif isinstance(value, str):
        output.write(value + "\n")
    else:
        output.write(json.dumps(value, sort_keys=True, default=str) + "\n")


def run_cli(layout: Mapping[str, Any] | str, registry: Mapping[str, ActionHandler], argv: Sequence[str] | None = None,
            *, input: TextIO | None = None, output: TextIO | None = None) -> int:
    """Run a declarative CLI.  Registry keys are the sole executable actions."""
    layout = load_layout(layout)
    actions = actions_from_layout(layout)
    parser = build_parser(layout, actions)
    args = parser.parse_args(argv)
    output = output or sys.stdout
    if args.command == "actions":
        if args.action_command == "list":
            needle = args.filter.lower()
            rows = [{"name": a.name, "description": a.description, "status": a.status, "tags": list(a.tags), "available": a.available, "planned": a.planned}
                    for a in actions.values() if (not needle or needle in (a.name + " " + a.description).lower())
                    and (args.status is None or a.status == args.status)
                    and (not args.tag or all(tag in a.tags for tag in args.tag))
                    and (not args.available or a.available)]
            _emit(rows, as_json=args.json, output=output)
            return 0
        action = actions[args.name]
        _emit({"name": action.name, "description": action.description, "status": action.status, "tags": list(action.tags), "available": action.available,
               "planned": action.planned, "confirmation": action.confirmation,
               "registry_binding": action.registry_binding,
               "parameters": action.parameters}, as_json=args.json, output=output)
        return 0
    action = actions[args.command]
    parameters = {name: getattr(args, name) for name in action.parameters if getattr(args, name, None) is not None}
    planned = {"status": "planned", "action": action.name, "parameters": parameters}
    if not action.available or action.planned:
        _emit({**planned, "reason": "unavailable"}, as_json=args.json, output=output)
        return 0
    if args.dry_run:
        _emit(planned, as_json=args.json, output=output)
        return 0
    if action.confirmation and not args.yes:
        reader = input or sys.stdin
        if not args.json:
            output.write(action.confirmation + " [y/N] ")
        if reader.readline().strip().lower() not in {"y", "yes"}:
            _emit({"status": "cancelled", "action": action.name}, as_json=args.json, output=output)
            return 0
    handler = registry.get(action.registry_binding or action.name)
    if handler is None:
        _emit({**planned, "reason": "unregistered"}, as_json=args.json, output=output)
        return 0
    _emit({"status": "completed", "action": action.name, "result": handler(parameters)}, as_json=args.json, output=output)
    return 0
