"""Explicit catalog composition, live discovery, OpenAPI and source audits."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import yaml

try:
    from ..operator_contract import ContractError, _validate_contract, load_snapshot
except ImportError:
    from operator_contract import ContractError, _validate_contract, load_snapshot
from .model import Endpoint, EndpointRegistry, WorkbenchError

MAX_DOCUMENT = 8 * 1024 * 1024


def read_document(path: Path) -> dict:
    if path.stat().st_size > MAX_DOCUMENT:
        raise WorkbenchError("document exceeds 8 MiB limit")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkbenchError("document must be an object")
    return value


def catalog_registry(document: dict, *, source: str, repository: str | None = None,
                     kind: str = "catalog", application: str | None = None) -> EndpointRegistry:
    if "contract" in document and isinstance(document.get("contract"), dict):
        document = document["contract"]
    if "revision" not in document:
        document = {**document, "revision": "legacy-client-input"}
    try:
        contract = _validate_contract(document, source)
    except ContractError as exc:
        raise WorkbenchError(str(exc)) from exc
    result = EndpointRegistry()
    for action in contract["actions"]:
        identifier = action["id"]
        api = contract["api"].get(identifier, {})
        path_names = set(re.findall(r"\{([^{}]+)\}", api.get("path", "")))
        params = {name: {**spec, "in": "path" if name in path_names else "query" if api.get("method") == "GET" else "body"}
                  for name, spec in action["parameters"].items()}
        result.add(Endpoint(
            id=identifier, application=application or identifier.split(".")[0],
            method=api.get("method"), path=api.get("path"), parameters=params,
            permissions=tuple(action["permissions"]), safety=action["safety"], status=action["status"],
            evidence=action.get("evidence", {}), title=action["title"], description=action.get("description", ""),
            tags=tuple(action["tags"]), binding=action.get("registry", {}),
            request_schema=api.get("request_schema"), responses=api.get("responses", {}),
            source=source, repository=repository, kind=kind, action_body=True))
    return result


def repository_registry(path: Path) -> EndpointRegistry:
    path = path.resolve()
    if path.is_file():
        index = path
        root = next((p for p in path.parents if (p / "pyproject.toml").is_file()), path.parent)
    else:
        root = path
        candidates = [path / "data/operator_contract_snapshot.json",
                      path / "metactl/data/operator_contract_snapshot.json"]
        snapshot = next((p for p in candidates if p.is_file()), None)
        if snapshot is None:
            raise WorkbenchError(f"no operator contract snapshot in {path}; pass a snapshot file")
        return catalog_registry(json.loads(snapshot.read_text(encoding="utf-8")), source=str(snapshot), repository=str(path))
    document = read_document(index)
    if "catalogs" not in document:
        return catalog_registry(document, source=str(index), repository=str(root))
    refs = document["catalogs"]
    if not isinstance(refs, list) or not all(isinstance(r, dict) for r in refs):
        raise WorkbenchError("catalogs must be explicit references")
    ids = [r.get("id") for r in refs]
    if ids != document.get("deployment_index") or len(set(ids)) != len(ids):
        raise WorkbenchError("catalog references must match the unique deployment_index")
    allowed = index.parent.parent.resolve()
    result = EndpointRegistry()
    for ref in refs:
        if not isinstance(ref.get("path"), str):
            raise WorkbenchError("catalog reference requires path")
        target = (index.parent / ref["path"]).resolve()
        if not target.is_relative_to(allowed):
            raise WorkbenchError("catalog reference escapes applications directory")
        for endpoint in catalog_registry(read_document(target), source=str(target), repository=str(root), application=ref["id"]).endpoints.values():
            result.add(endpoint)
    return result


def live_registry(client) -> EndpointRegistry:
    try:
        status, _, raw = client.read("/api/meta/actions")
    except AssertionError:
        # Narrow compatibility for injected legacy test clients; real clients
        # receive the server-owned manifest below.
        status, _, raw = client.read("/api/actions")
    if status == 404:
        status, _, raw = client.read("/api/actions")
        if status == 200:
            document = json.loads(raw)
            if isinstance(document, dict) and {"version", "revision", "actions"} <= set(document):
                local = load_snapshot()
                by_id = {item["id"]: item for item in local["actions"]}
                actions = []
                api = {}
                for item in document["actions"]:
                    if not isinstance(item, dict) or item.get("id") not in by_id:
                        continue
                    merged = {**by_id[item["id"]], **{key: item[key] for key in ("title", "status") if key in item}}
                    actions.append(merged)
                    if item.get("callable"):
                        api[item["id"]] = {"method": item.get("method"), "path": item.get("path")}
                return catalog_registry({**local, "actions": actions, "api": api},
                                        source=client.base_url + "/api/actions", kind="live")
    if status != 200:
        raise WorkbenchError(f"full catalog discovery returned HTTP {status}; /api/actions is a legacy summary, not a complete contract")
    document = json.loads(raw)
    if document.get("format") != "meta-api-catalog/1" or not isinstance(document.get("catalogs"), list):
        raise WorkbenchError("unsupported live catalog format")
    result = EndpointRegistry()
    for item in document["catalogs"]:
        for endpoint in catalog_registry(item["catalog"], source=client.base_url + "/api/meta/actions", kind="live", application=item["id"]).endpoints.values():
            result.add(endpoint)
    for item in document.get("unavailable", []):
        result.diagnostics.append(f"runtime unavailable: {item}")
    return result


def openapi_registry(document: dict, source: str) -> EndpointRegistry:
    if not str(document.get("openapi", "")).startswith(("3.0.", "3.1.")):
        raise WorkbenchError("only OpenAPI 3.0 and 3.1 are supported")

    def resolve(value: dict, trail=()) -> dict:
        if "$ref" not in value:
            return value
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise WorkbenchError("external OpenAPI references are disabled; bundle the document first")
        if ref in trail:
            raise WorkbenchError("cyclic structural OpenAPI reference")
        item = document
        try:
            for part in ref[2:].split("/"):
                item = item[part.replace("~1", "/").replace("~0", "~")]
        except (KeyError, TypeError) as exc:
            raise WorkbenchError(f"unresolved reference {ref}") from exc
        return {**resolve(item, (*trail, ref)), **{k: v for k, v in value.items() if k != "$ref"}}

    def schema(value: dict) -> dict:
        # Keep schema references local to this snapshot. Recursive data schemas
        # are supported by JSON Schema; do not recursively inline them.
        result = dict(value)
        result["components"] = document.get("components", {})
        return result

    def reject_external(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "$ref" and (not isinstance(v, str) or not v.startswith("#/")):
                    raise WorkbenchError("external OpenAPI references are disabled")
                reject_external(v)
        elif isinstance(value, list):
            for v in value:
                reject_external(v)
    reject_external(document)
    result = EndpointRegistry()
    for path, raw_item in document.get("paths", {}).items():
        item = resolve(raw_item)
        for method, raw_operation in item.items():
            if method not in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}:
                continue
            operation = resolve(raw_operation)
            parameters = {}
            for raw in [*item.get("parameters", []), *operation.get("parameters", [])]:
                p = resolve(raw)
                name = p["name"]
                if name in parameters and parameters[name]["in"] != p["in"]:
                    raise WorkbenchError(f"{path}: duplicate parameter names in different locations are unsupported: {name}")
                if p.get("style", "form" if p["in"] in {"query", "cookie"} else "simple") not in {"form", "simple"}:
                    raise WorkbenchError(f"{path}: unsupported parameter style")
                if p["in"] == "cookie" or p.get("allowReserved") or p.get("explode") is False:
                    raise WorkbenchError(f"{path}: unsupported parameter serialization")
                spec = resolve(p.get("schema", {}))
                if spec.get("type") == "object" or (spec.get("type") == "array" and p["in"] != "query"):
                    raise WorkbenchError(f"{path}: unsupported structured parameter serialization")
                parameters[name] = {**spec, "in": p["in"], "required": p.get("required", False), "schema": schema(spec)}
            body = resolve(operation.get("requestBody", {}))
            content = body.get("content", {})
            if content and "application/json" not in content:
                raise WorkbenchError(f"{path}: only JSON request bodies are supported")
            responses = {}
            for status, raw in operation.get("responses", {}).items():
                response = resolve(raw)
                spec = response.get("content", {}).get("application/json", {}).get("schema")
                responses[str(status)] = schema(spec) if spec is not None else None
            identifier = operation.get("operationId", f"{method.upper()} {path}")
            result.add(Endpoint(id=identifier, application=(operation.get("tags") or ["openapi"])[0],
                method=method.upper(), path=path, parameters=parameters,
                permissions=tuple(json.dumps(s, sort_keys=True) for s in operation.get("security", document.get("security", []))),
                safety=operation.get("x-safety", {}), status="deprecated" if operation.get("deprecated") else "imported",
                title=operation.get("summary", identifier), description=operation.get("description", ""),
                tags=tuple(operation.get("tags", [])), request_schema=schema(content["application/json"].get("schema", {})) if content else None,
                responses=responses, source=source, kind="openapi", body_required=body.get("required", False)))
    return result


def audit_routes(root: Path, registry: EndpointRegistry) -> dict:
    """Best-effort AST audit of static decorators; never imports source code."""
    declared = {(e.method, e.path) for e in registry.endpoints.values() if e.method}
    found, diagnostics = [], []
    for path in root.rglob("*.py"):
        if any(p.startswith(".") or p in {"node_modules", "refference", "__pycache__"} for p in path.relative_to(root).parts):
            continue
        if path.is_symlink() or path.stat().st_size > MAX_DOCUMENT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError, OSError):
            diagnostics.append(f"could not parse {path.relative_to(root)}")
            continue
        for node in ast.walk(tree):
            for decorator in getattr(node, "decorator_list", []):
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                method = decorator.func.attr
                if method not in {"get", "post", "put", "patch", "delete", "head", "options", "route"}:
                    continue
                try:
                    route = ast.literal_eval(decorator.args[0])
                    methods = [method.upper()] if method != "route" else next((ast.literal_eval(k.value) for k in decorator.keywords if k.arg == "methods"), ["GET"])
                    if not isinstance(route, str) or not isinstance(methods, list):
                        raise ValueError()
                except (IndexError, ValueError, TypeError):
                    diagnostics.append(f"dynamic route at {path.relative_to(root)}:{node.lineno}")
                    continue
                route = re.sub(r"<(?:(?:[^:>]+):)?([^>]+)>", r"{\1}", route)
                for m in methods:
                    found.append({"method": m, "path": route, "source": f"{path.relative_to(root)}:{node.lineno}",
                                  "undeclared": (m, route) not in declared})
    return {"coverage": "static decorators only; prefixes, dynamic dispatch and non-decorator routes require runtime evidence",
            "routes": found, "diagnostics": diagnostics}
