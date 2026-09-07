"""Normalized endpoint contracts and semantic drift, with no UI dependency."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json
import re
from typing import Any


class WorkbenchError(ValueError):
    pass


@dataclass(frozen=True)
class Endpoint:
    id: str
    application: str
    method: str | None
    path: str | None
    parameters: dict[str, dict] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    safety: dict = field(default_factory=dict)
    status: str = "unknown"
    evidence: dict = field(default_factory=dict)
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    binding: dict = field(default_factory=dict)
    request_schema: dict | None = None
    responses: dict = field(default_factory=dict)
    source: str = ""
    kind: str = "catalog"
    # Only local catalogs can provide executable evidence references.
    repository: str | None = None
    action_body: bool = False
    body_required: bool = False

    @property
    def effect(self) -> str:
        explicit = self.safety.get("effect")
        if explicit in {"read", "mutation", "destructive", "hardware"}:
            if explicit == "read" and self.method not in {"GET", "HEAD", "OPTIONS"}:
                return "unknown"
            return explicit
        if self.safety.get("confirmation") == "physical" or "hardware" in self.tags:
            return "hardware"
        if self.method in {"GET", "HEAD", "OPTIONS"} and self.safety.get("confirmation", "none") == "none":
            return "read"
        # Legacy catalogs do not distinguish run control from other writes.
        # Treat unknown writes as potentially physical, never infer safety
        # from endpoint names or HTTP success.
        return "unknown"

    @property
    def badge(self) -> str:
        return {"read": "R", "mutation": "M", "destructive": "D", "hardware": "HW", "unknown": "HW?"}[self.effect]

    def contract(self) -> dict:
        value = asdict(self)
        for key in ("source", "kind", "repository"):
            value.pop(key)
        return value


@dataclass
class EndpointRegistry:
    endpoints: dict[str, Endpoint] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def add(self, endpoint: Endpoint) -> None:
        if endpoint.id in self.endpoints:
            raise WorkbenchError(f"duplicate action id: {endpoint.id}")
        if endpoint.path is not None:
            if not endpoint.path.startswith("/") or endpoint.path.startswith("//") or "?" in endpoint.path or "#" in endpoint.path:
                raise WorkbenchError(f"invalid endpoint path: {endpoint.id}")
            names = re.findall(r"\{([^{}]+)\}", endpoint.path)
            for name in names:
                spec = endpoint.parameters.get(name)
                if not spec or spec.get("in") != "path" or not spec.get("required"):
                    raise WorkbenchError(f"{endpoint.id}: undeclared required path parameter {name}")
        self.endpoints[endpoint.id] = endpoint

    def select(self, query: str = "", application: str | None = None) -> list[Endpoint]:
        words = query.lower().split()
        return [e for e in self.endpoints.values()
                if (not application or e.application == application)
                and all(w in " ".join((e.id, e.title, e.method or "", e.path or "", e.status, e.badge, *e.tags)).lower() for w in words)]


def compare(declared: EndpointRegistry, live: EndpointRegistry) -> list[dict]:
    result = []
    for identifier in sorted(declared.endpoints.keys() | live.endpoints.keys()):
        left, right = declared.endpoints.get(identifier), live.endpoints.get(identifier)
        # Actions explicitly lacking an API are informative, not missing routes.
        if right is None:
            if left.method:
                result.append({"id": identifier, "kind": "declared_only"})
        elif left is None:
            result.append({"id": identifier, "kind": "live_only"})
        else:
            a, b = left.contract(), right.contract()
            fields = [k for k in a if json.dumps(a[k], sort_keys=True) != json.dumps(b[k], sort_keys=True)]
            if fields:
                result.append({"id": identifier, "kind": "changed", "fields": fields,
                               "declared": {k: a[k] for k in fields}, "live": {k: b[k] for k in fields}})
    return result
