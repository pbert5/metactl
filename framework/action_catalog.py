"""Strict, generic contract for declarative action catalogs.

The catalog is a description of available actions, not an execution registry.
In particular, ``registry.binding`` names a separately-owned implementation
entry and does not cause this module to import or invoke it.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Mapping

import yaml


class ActionCatalogError(ValueError):
    """Raised when an action catalog violates the declarative contract."""


_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_TOP_LEVEL = {"version", "deployment_index", "actions", "api"}
_ACTION = {"id", "title", "description", "tags", "status", "registry", "evidence", "safety", "permissions", "parameters", "replacement"}
_REGISTRY = {"id", "binding"}
_EVIDENCE = {"kind", "source", "tests"}
_SAFETY = {"risk", "confirmation", "reversible"}
_PARAMETER = {"type", "required", "description", "default", "enum"}
_STATUSES = {"planned", "experimental", "implemented", "deprecated", "partial", "superseded"}
_EVIDENCE_KINDS = {"software", "configuration", "simulator", "physical", "calibrated"}
_PARAMETER_TYPES = {"string", "integer", "number", "boolean", "object", "array", "json"}


@dataclass(frozen=True)
class ActionCatalog:
    """Validated catalog in deterministic deployment order."""

    version: str
    deployment_index: tuple[str, ...]
    actions: tuple[dict[str, Any], ...]
    api: dict[str, dict[str, Any]]

    def action(self, identifier: str) -> dict[str, Any] | None:
        return next((action for action in self.actions if action["id"] == identifier), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "deployment_index": list(self.deployment_index),
            "actions": [dict(action) for action in self.actions],
            "api": {key: dict(value) for key, value in self.api.items()},
        }


def load_action_catalog(path: Path) -> ActionCatalog:
    """Load JSON or YAML from *path* and return its validated catalog."""
    path = Path(path)
    if not path.is_file():
        raise ActionCatalogError(f"catalog does not exist: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ActionCatalogError(f"{path}: invalid YAML: {error}") from error
    return parse_action_catalog(document, source=path)


def parse_action_catalog(document: Mapping[str, Any], *, source: Path | str = "catalog") -> ActionCatalog:
    """Validate a decoded catalog and normalize actions by its explicit index."""
    label = str(source)
    _mapping(document, label)
    _keys(document, _TOP_LEVEL, label)
    version = document.get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ActionCatalogError(f"{label}: version must be a semantic version string")
    index = document.get("deployment_index")
    if not isinstance(index, list) or not index or not all(isinstance(item, str) and _IDENTIFIER.fullmatch(item) for item in index):
        raise ActionCatalogError(f"{label}: deployment_index must be a non-empty list of action ids")
    if len(index) != len(set(index)):
        raise ActionCatalogError(f"{label}: deployment_index contains duplicate action ids")
    raw_actions = document.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ActionCatalogError(f"{label}: actions must be a non-empty list")

    by_id: dict[str, dict[str, Any]] = {}
    for position, raw in enumerate(raw_actions):
        action_label = f"{label}: actions[{position}]"
        _mapping(raw, action_label)
        _keys(raw, _ACTION, action_label)
        identifier = raw.get("id")
        if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
            raise ActionCatalogError(f"{action_label}: id must be a non-empty identifier")
        if identifier in by_id:
            raise ActionCatalogError(f"{label}: duplicate action id {identifier!r}")
        _validate_action(raw, action_label)
        by_id[identifier] = dict(raw)
    if set(index) != set(by_id):
        missing = sorted(set(index) - set(by_id))
        extra = sorted(set(by_id) - set(index))
        raise ActionCatalogError(f"{label}: deployment_index mismatch (missing={missing}, extra={extra})")
    api = document.get("api", {})
    if not isinstance(api, Mapping):
        raise ActionCatalogError(f"{label}: api must be an object")
    for identifier, contract in api.items():
        api_label = f"{label}.api[{identifier!r}]"
        if identifier not in by_id:
            raise ActionCatalogError(f"{api_label}: unknown action id")
        _mapping(contract, api_label)
        _keys(contract, {"method", "path", "operator_api"}, api_label)
        if contract.get("operator_api", True) is not True:
            raise ActionCatalogError(f"{api_label}.operator_api must be true")
        if contract.get("method") not in {"GET", "POST", "PATCH", "DELETE"}:
            raise ActionCatalogError(f"{api_label}.method must be an HTTP method")
        path = contract.get("path")
        if not isinstance(path, str) or not path.startswith("/api/") or "{" in path and "}" not in path:
            raise ActionCatalogError(f"{api_label}.path must be an /api/ path template")
        placeholders = set(re.findall(r"\{([^{}]+)\}", path))
        parameters = by_id[identifier].get("parameters", {})
        unknown = placeholders - set(parameters)
        if unknown:
            raise ActionCatalogError(f"{api_label}: path parameters not declared: {sorted(unknown)}")
        missing = {name for name in placeholders if not isinstance(parameters.get(name), Mapping) or parameters[name].get("required") is not True}
        if missing:
            raise ActionCatalogError(f"{api_label}: path parameters must be declared required: {sorted(missing)}")
    return ActionCatalog(version, tuple(index), tuple(by_id[identifier] for identifier in index), {key: dict(value) for key, value in api.items()})


def validate_action_catalog(document: Mapping[str, Any], *, source: Path | str = "catalog") -> None:
    """Raise :class:`ActionCatalogError` unless *document* is valid."""
    parse_action_catalog(document, source=source)


def _validate_action(action: Mapping[str, Any], label: str) -> None:
    # Application catalogs may use the title as their short operator
    # description; retain the strict non-empty requirement while avoiding
    # duplicated prose in compact declarative catalogs.
    if "description" not in action and isinstance(action.get("title"), str):
        action = {**action, "description": action["title"]}
    for field in ("title", "description", "status"):
        if not isinstance(action.get(field), str) or not action[field]:
            raise ActionCatalogError(f"{label}: {field} must be a non-empty string")
    if not isinstance(action.get("tags"), list) or not action["tags"] or not all(isinstance(tag, str) and tag for tag in action["tags"]):
        raise ActionCatalogError(f"{label}: tags must be a non-empty list of strings")
    if action["status"] not in _STATUSES:
        raise ActionCatalogError(f"{label}: unsupported status {action['status']!r}")
    registry = action.get("registry")
    if action["status"] in {"experimental", "implemented"} and registry is None:
        raise ActionCatalogError(f"{label}: {action['status']} actions require an implementation registry")
    if registry is not None:
        _mapping(registry, f"{label}.registry")
        _keys(registry, _REGISTRY, f"{label}.registry")
        for field in _REGISTRY:
            if not isinstance(registry.get(field), str) or not registry[field]:
                raise ActionCatalogError(f"{label}.registry.{field} must be a non-empty string")
    evidence = action.get("evidence")
    if action["status"] == "implemented" and evidence is None:
        raise ActionCatalogError(f"{label}: implemented actions require evidence metadata")
    if evidence is not None:
        _mapping(evidence, f"{label}.evidence")
        _keys(evidence, _EVIDENCE, f"{label}.evidence")
        if evidence.get("kind") not in _EVIDENCE_KINDS or not isinstance(evidence.get("source"), str) or not evidence["source"]:
            raise ActionCatalogError(f"{label}.evidence requires kind and source")
        tests = evidence.get("tests", [])
        if not isinstance(tests, list) or not tests or not all(isinstance(item, str) and item for item in tests):
            raise ActionCatalogError(f"{label}.evidence.tests must be a non-empty list of strings")
    if action["status"] == "deprecated" and not isinstance(action.get("replacement"), str):
        raise ActionCatalogError(f"{label}: deprecated actions require replacement")
    safety = action.get("safety")
    _mapping(safety, f"{label}.safety")
    _keys(safety, _SAFETY, f"{label}.safety")
    if safety.get("risk") not in {"low", "medium", "high"} or safety.get("confirmation") not in {"none", "operator", "physical", "required"} or not isinstance(safety.get("reversible"), bool):
        raise ActionCatalogError(f"{label}.safety has invalid risk, confirmation, or reversible value")
    permissions = action.get("permissions")
    if not isinstance(permissions, list) or not all(isinstance(item, str) and item for item in permissions) or len(permissions) != len(set(permissions)):
        raise ActionCatalogError(f"{label}.permissions must be a list of unique non-empty strings")
    parameters = action.get("parameters")
    if not isinstance(parameters, dict):
        raise ActionCatalogError(f"{label}.parameters must be an object")
    for name, parameter in parameters.items():
        parameter_label = f"{label}.parameters[{name!r}]"
        if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
            raise ActionCatalogError(f"{parameter_label}: invalid parameter name")
        _mapping(parameter, parameter_label)
        _keys(parameter, _PARAMETER, parameter_label)
        if parameter.get("type") not in _PARAMETER_TYPES or ("required" in parameter and not isinstance(parameter.get("required"), bool)):
            raise ActionCatalogError(f"{parameter_label} requires a supported type and optional boolean required")
        if "enum" in parameter and (not isinstance(parameter["enum"], list) or not parameter["enum"]):
            raise ActionCatalogError(f"{parameter_label}.enum must be a non-empty list")


def _mapping(value: Any, label: str) -> None:
    if not isinstance(value, Mapping):
        raise ActionCatalogError(f"{label} must be an object")


def _keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if not all(isinstance(key, str) for key in value):
        raise ActionCatalogError(f"{label}: field names must be strings")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ActionCatalogError(f"{label}: unknown field(s): {', '.join(unknown)}")
