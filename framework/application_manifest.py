"""Explicit application composition without a second page language."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class CompositionError(ValueError):
    """Raised when an application composition is malformed or ambiguous."""


@dataclass(frozen=True)
class ApplicationManifest:
    id: str
    title: str
    status: str
    source_root: Path
    owns: tuple[str, ...]
    resources: tuple[str, ...]
    extensions: tuple[str, ...]
    manifest_path: Path
    repository_root: Path = Path(".")
    interfaces: tuple[dict[str, Any], ...] = ()
    capabilities: dict[str, Any] = field(default_factory=dict)
    permissions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    runtime_services: tuple[str, ...] = ()
    adapter: dict[str, Any] | None = None

    @property
    def allowed_root(self) -> Path:
        """Canonical directory owned by this application."""
        return self.source_root

    def resource_paths(self) -> tuple[Path, ...]:
        return tuple(resolve_trusted_path(self.source_root, resource, self.allowed_root, kind="resource") for resource in self.resources)

    def repository_relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.repository_root).as_posix()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "manifest": self.repository_relative(self.manifest_path),
            "sourceRoot": self.repository_relative(self.source_root),
            "owns": list(self.owns),
            "resources": [self.repository_relative(path) for path in self.resource_paths()],
            "extensions": list(self.extensions),
            "interfaces": [dict(interface) for interface in self.interfaces],
            "capabilities": dict(self.capabilities or {}),
            "permissions": list(self.permissions),
            "dependencies": list(self.dependencies),
            "runtimeServices": list(self.runtime_services),
            **({"adapter": dict(self.adapter)} if self.adapter else {}),
        }


@dataclass(frozen=True)
class ApplicationComposition:
    id: str
    applications: tuple[ApplicationManifest, ...]

    @property
    def extension_owners(self) -> dict[str, str]:
        return {extension: application.id for application in self.applications for extension in application.extensions}

    def application(self, identifier: str) -> ApplicationManifest | None:
        return next((item for item in self.applications if item.id == identifier), None)


def load_composition(path: Path) -> ApplicationComposition:
    """Load and validate an ordered deployment composition."""
    composition_path = path.resolve()
    document = _load_mapping(composition_path)
    composition_id = document.get("id")
    if not isinstance(composition_id, str) or not composition_id:
        raise CompositionError(f"{composition_path}: id must be a non-empty string")
    refs = document.get("manifests")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise CompositionError(f"{composition_path}: manifests must be a list of paths")
    repository_root = composition_path.parent.parent.resolve()
    manifests = tuple(_load_manifest(resolve_trusted_path(composition_path.parent, ref, repository_root, kind="nested manifest"), repository_root) for ref in refs)
    return compose_applications(composition_id, manifests)


def compose_applications(composition_id: str, manifests: tuple[ApplicationManifest, ...] | list[ApplicationManifest]) -> ApplicationComposition:
    if not composition_id:
        raise CompositionError("composition id must not be empty")
    ordered = tuple(manifests)
    ids = [manifest.id for manifest in ordered]
    if len(ids) != len(set(ids)):
        duplicates = sorted({identifier for identifier in ids if ids.count(identifier) > 1})
        raise CompositionError(f"duplicate application ids: {', '.join(duplicates)}")
    extension_ids: dict[str, str] = {}
    for manifest in ordered:
        repository_root = manifest.repository_root.resolve()
        if not _is_within(manifest.manifest_path, repository_root) or not _is_within(manifest.source_root, repository_root):
            raise CompositionError(f"{manifest.manifest_path}: manifest ownership escapes repository root")
        for path in manifest.resource_paths():
            if not _is_within(path, manifest.allowed_root):
                raise CompositionError(f"{manifest.manifest_path}: resource escapes application root: {path}")
            if not path.exists():
                raise CompositionError(f"{manifest.manifest_path}: resource does not exist: {path}")
        for extension in manifest.extensions:
            previous = extension_ids.setdefault(extension, manifest.id)
            if previous != manifest.id:
                raise CompositionError(f"extension {extension!r} is owned by both {previous!r} and {manifest.id!r}")
        _validate_interfaces(manifest)
    return ApplicationComposition(composition_id, ordered)


def _load_manifest(path: Path, repository_root: Path) -> ApplicationManifest:
    if not _is_within(path, repository_root):
        raise CompositionError(f"{path}: nested manifest escapes repository root")
    document = _load_mapping(path)
    identifier = document.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise CompositionError(f"{path}: id must be a non-empty string")
    source_ref = document.get("source_root")
    if not isinstance(source_ref, str):
        raise CompositionError(f"{path}: source_root must be a relative path")
    if Path(source_ref).is_absolute():
        raise CompositionError(f"{path}: source_root must be relative")
    source_root = resolve_trusted_path(path.parent, source_ref, repository_root, kind="source_root")
    if not source_root.is_dir():
        raise CompositionError(f"{path}: source_root does not exist: {source_root}")
    if not _is_within(source_root, repository_root):
        raise CompositionError(f"{path}: source_root escapes repository root: {source_ref}")
    return ApplicationManifest(
        id=identifier,
        title=str(document.get("title", identifier)),
        status=str(document.get("status", "implemented")),
        source_root=source_root,
        owns=_string_tuple(document, "owns", path),
        resources=_string_tuple(document, "resources", path),
        extensions=_string_tuple(document, "extensions", path),
        manifest_path=path,
        repository_root=repository_root,
        interfaces=_interfaces(document.get("interfaces", []), path),
        capabilities=_capabilities(document.get("capabilities", {}), path),
        permissions=_string_tuple(document, "permissions", path),
        dependencies=_string_tuple(document, "dependencies", path),
        runtime_services=_runtime_services(document, path),
        adapter=_optional_mapping(document, "adapter", path),
    )


def _interfaces(value: Any, path: Path) -> tuple[dict[str, Any], ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise CompositionError(f"{path}: interfaces must be a list of objects")
    result = []
    ids: set[str] = set()
    for item in value:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise CompositionError(f"{path}: interface id must be a non-empty string")
        if identifier in ids:
            raise CompositionError(f"{path}: duplicate interface id {identifier!r}")
        ids.add(identifier)
        result.append(dict(item))
    return tuple(result)


def _capabilities(value: Any, path: Path) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise CompositionError(f"{path}: capabilities must be an object")
    for key in ("requires", "optional", "fallbacks"):
        entries = value.get(key, [])
        if not isinstance(entries, list) or not all(isinstance(item, str) and item for item in entries):
            raise CompositionError(f"{path}: capabilities.{key} must be a list of non-empty strings")
    return {key: list(value.get(key, [])) for key in ("requires", "optional", "fallbacks")}


def _optional_mapping(document: dict[str, Any], key: str, path: Path) -> dict[str, Any] | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CompositionError(f"{path}: {key} must be an object")
    result = dict(value)
    if key == "adapter":
        mode = result.get("mode")
        if mode not in {"api"}:
            raise CompositionError(f"{path}: adapter.mode is unsupported")
        upstream = result.get("upstream")
        if not isinstance(upstream, str) or not upstream or any(token in upstream for token in ("://", "/", "\\")):
            raise CompositionError(f"{path}: adapter.upstream must be a registered service id")
        forbidden = {"module", "implementation", "entrypoint", "script", "url", "target"}
        if forbidden.intersection(result):
            raise CompositionError(f"{path}: adapter may not declare implementation or arbitrary proxy targets")
    return result


def _validate_interfaces(manifest: ApplicationManifest) -> None:
    seen_routes: set[str] = set()
    seen_pages: set[str] = set()
    for interface in manifest.interfaces:
        for page in interface.get("pages", []):
            if not isinstance(page, dict) or not isinstance(page.get("id"), str):
                raise CompositionError(f"{manifest.manifest_path}: interface pages must have string ids")
            if page["id"] in seen_pages:
                raise CompositionError(f"{manifest.manifest_path}: duplicate page id {page['id']!r}")
            seen_pages.add(page["id"])
            route = page.get("route")
            if route is not None:
                if not isinstance(route, str) or not route.startswith("/") or ".." in route.split("/"):
                    raise CompositionError(f"{manifest.manifest_path}: invalid interface route {route!r}")
                if route in seen_routes:
                    raise CompositionError(f"{manifest.manifest_path}: duplicate interface route {route!r}")
                seen_routes.add(route)


def _load_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CompositionError(f"manifest does not exist: {path}")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise CompositionError(f"{path}: invalid YAML: {error}") from error
    if not isinstance(value, dict):
        raise CompositionError(f"{path}: expected a mapping")
    return value


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_trusted_path(base: Path, reference: str, allowed_root: Path, *, kind: str) -> Path:
    """Resolve a manifest reference and verify its post-symlink ownership.

    In-tree symlinks remain valid compatibility aliases; only their canonical
    destination matters.  ``strict=False`` lets us report missing resources
    consistently while still resolving every existing symlink component.
    """
    if not isinstance(reference, str) or not reference or Path(reference).is_absolute():
        raise CompositionError(f"{base}: {kind} must be a relative path")
    candidate = (base / reference).resolve(strict=False)
    if not _is_within(candidate, allowed_root):
        boundary = "repository root" if kind in {"source_root", "nested manifest"} else "application root"
        raise CompositionError(f"{base}: {kind} escapes {boundary}: {reference}")
    return candidate


def _string_tuple(document: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    value = document.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise CompositionError(f"{path}: {key} must be a list of non-empty strings")
    return tuple(value)


def _runtime_services(document: dict[str, Any], path: Path) -> tuple[str, ...]:
    services = _string_tuple(document, "runtime_services", path)
    if len(set(services)) != len(services):
        raise CompositionError(f"{path}: runtime_services must not contain duplicates")
    return services
