"""Server-owned operator contract consumed as a client-side snapshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


class ContractError(ValueError):
    """The snapshot or live contract cannot safely drive an operator client."""


SNAPSHOT_PATH = Path(__file__).with_name("data") / "operator_contract_snapshot.json"


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{label} must be an object")
    return value


def _validate_contract(value: Any, label: str) -> dict[str, Any]:
    document = dict(_mapping(value, label))
    for name in ("version", "revision", "actions", "api"):
        if name not in document:
            raise ContractError(f"{label} is missing {name}")
    if not isinstance(document["version"], str) or not document["version"]:
        raise ContractError(f"{label}.version must be a string")
    if not isinstance(document["revision"], str) or not document["revision"]:
        raise ContractError(f"{label}.revision must be a string")
    actions = document["actions"]
    if not isinstance(actions, list) or not actions:
        raise ContractError(f"{label}.actions must be a non-empty list")
    identifiers: set[str] = set()
    for position, raw in enumerate(actions):
        item = _mapping(raw, f"{label}.actions[{position}]")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ContractError(f"{label}.actions[{position}].id is invalid")
        identifiers.add(identifier)
        if not isinstance(item.get("title"), str) or not isinstance(item.get("status"), str):
            raise ContractError(f"{label}.actions[{position}] has invalid title/status")
        if not isinstance(item.get("permissions", []), list) or not isinstance(item.get("safety", {}), Mapping):
            raise ContractError(f"{label}.actions[{position}] has invalid metadata")
        if not isinstance(item.get("parameters", {}), Mapping):
            raise ContractError(f"{label}.actions[{position}].parameters must be an object")
    api = _mapping(document["api"], f"{label}.api")
    if set(api) - identifiers:
        raise ContractError(f"{label}.api contains unknown actions")
    for identifier, raw in api.items():
        binding = _mapping(raw, f"{label}.api[{identifier}]")
        if binding.get("method") not in {"GET", "POST", "PATCH", "DELETE"}:
            raise ContractError(f"{label}.api[{identifier}].method is invalid")
        if not isinstance(binding.get("path"), str) or not binding["path"].startswith("/api/"):
            raise ContractError(f"{label}.api[{identifier}].path is invalid")
    return document


def load_snapshot(path: Path | str = SNAPSHOT_PATH) -> dict[str, Any]:
    path = Path(path)
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"operator contract snapshot is unavailable or malformed: {path}") from exc
    if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("provenance"), Mapping):
        raise ContractError("operator contract snapshot has invalid provenance")
    provenance = snapshot["provenance"]
    for name in ("generator", "source", "source_revision"):
        if not isinstance(provenance.get(name), str) or not provenance[name]:
            raise ContractError(f"operator contract snapshot provenance lacks {name}")
    return _validate_contract(snapshot.get("contract"), "operator contract snapshot.contract")


def build_manifest(contract: Mapping[str, Any]) -> dict[str, Any]:
    actions = {item["id"]: item for item in contract["actions"]}
    manifest_actions = []
    for identifier in contract["actions"]:
        item = actions[identifier["id"]]
        binding = contract["api"].get(item["id"])
        manifest_actions.append({
            "id": item["id"], "title": item["title"], "status": item["status"],
            "callable": binding is not None, "method": binding.get("method") if binding else None,
            "path": binding.get("path") if binding else None,
            "permissions": list(item.get("permissions", [])), "safety": item.get("safety", {}),
        })
    return {"version": contract["version"], "revision": contract["revision"], "actions": manifest_actions}


def _manifest_fingerprint(value: Mapping[str, Any]) -> str:
    actions = value.get("actions", [])
    normalized = []
    for item in actions:
        if not isinstance(item, Mapping):
            raise ContractError("live operator contract has malformed action metadata")
        normalized.append({key: item.get(key) for key in ("id", "status", "callable", "method", "path", "permissions", "safety")})
    return hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def contract_drift(local: Mapping[str, Any], live: Mapping[str, Any] | None) -> str:
    if not isinstance(live, Mapping) or not isinstance(live.get("actions"), list):
        return "unavailable"
    if live.get("version") != local.get("version") or live.get("revision") != local.get("revision"):
        return "incompatible"
    expected = build_manifest(local)
    return "clean" if _manifest_fingerprint(expected) == _manifest_fingerprint(live) else "changed"


def ensure_mutation_compatible(local: Mapping[str, Any], live: Mapping[str, Any] | None) -> None:
    status = contract_drift(local, live)
    if status != "clean":
        raise ContractError(f"live operator contract is {status}; mutation is blocked")

