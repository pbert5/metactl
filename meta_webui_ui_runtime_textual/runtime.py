"""Generic, safe Textual rendering of the Application → Page → Frame tree."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


Json = dict[str, Any]
SourceResolver = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]
ActionDispatcher = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]


def _value(value: Any, scope: Mapping[str, Any]) -> Any:
    """Resolve only declarative ``from`` bindings; YAML never executes code."""
    if not isinstance(value, Mapping) or not isinstance(value.get("from"), str):
        return value
    current: Any = scope
    for segment in value["from"].split("."):
        current = current.get(segment) if isinstance(current, Mapping) else None
    return current


def _source(node: Mapping[str, Any], scope: Mapping[str, Any], resolver: SourceResolver | None) -> Any:
    source = node.get("source")
    if not isinstance(source, Mapping):
        return None
    if "value" in source:
        return _value(source["value"], scope)
    return resolver(source, scope) if resolver else None


def render_document(document: Mapping[str, Any], *, page_id: str, scope: Mapping[str, Any] | None = None,
                    source_resolver: SourceResolver | None = None) -> list["Widget"]:
    """Render one configured page.  This is intentionally a narrow parity set.

    The function is separately testable and forms the shared bridge used by
    online (central source adapter) and offline (EdgeStore source adapter)
    applications.
    """
    pages = document.get("pages", {})
    page = pages.get(page_id) if isinstance(pages, Mapping) else None
    if not isinstance(page, Mapping):
        raise KeyError(f"configured page not found: {page_id}")
    return _node(page.get("content", {}), dict(scope or {}), source_resolver)


def _node(node: Any, scope: Mapping[str, Any], resolver: SourceResolver | None) -> list["Widget"]:
    from textual.widgets import DataTable, Label, Pretty, Static
    from textual.containers import Container, Horizontal, Vertical

    if not isinstance(node, Mapping):
        return []
    if "frame" in node:
        frame = node["frame"]
        if not isinstance(frame, Mapping): return []
        children = _node(frame.get("content", []), scope, resolver)
        return [Horizontal(*children) if frame.get("direction") == "horizontal" else Vertical(*children)]
    if "pane" in node:
        pane = node["pane"]
        if not isinstance(pane, Mapping): return []
        title = str(pane.get("title", ""))
        return [Container(Label(title, classes="pane-title"), *_node(pane.get("content", {}), scope, resolver), classes="pane")]
    if "element" not in node:
        if isinstance(node.get("content"), list):
            return [child for item in node["content"] for child in _node(item, scope, resolver)]
        return []
    element = node["element"]
    if not isinstance(element, Mapping): return []
    use, options, data = element.get("use"), element.get("options", {}), _source(element, scope, resolver)
    options = options if isinstance(options, Mapping) else {}
    if use == "text":
        lines = [str(options.get("summary", options.get("text", ""))), *map(str, options.get("items", []))]
        return [Static("\n".join(line for line in lines if line))]
    if use in {"pretty-data-view", "status"}:
        return [Pretty(data if data is not None else options)]
    if use == "sheet":
        table = DataTable()
        columns = options.get("columns", [])
        if isinstance(columns, list):
            table.add_columns(*(str(column.get("title", column.get("id", ""))) for column in columns if isinstance(column, Mapping)))
            for record in data if isinstance(data, list) else []:
                if isinstance(record, Mapping):
                    table.add_row(*(str(_path(record, str(column.get("path", column.get("id", ""))))) or "" for column in columns if isinstance(column, Mapping)))
        return [table]
    # A declared extension without a Textual renderer is an explicit safe
    # escape hatch, not an attempt to import a React implementation.
    return [Static(f"{use or 'element'} is unavailable in this Textual runtime", classes="unsupported-element")]


def _path(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for segment in path.split("."):
        current = current.get(segment) if isinstance(current, Mapping) else None
    return current


@dataclass
class ApplicationLoader:
    """Application-neutral loader with injectable data and action adapters."""
    document: Mapping[str, Any]
    source_resolver: SourceResolver | None = None
    action_dispatcher: ActionDispatcher | None = None

    def application(self, page_id: str, *, scope: Mapping[str, Any] | None = None) -> "TextualApplication":
        return TextualApplication(self, page_id, dict(scope or {}))


class TextualApplication:  # Textual's App is composed lazily to keep imports optional for tooling.
    def __new__(cls, loader: ApplicationLoader, page_id: str, scope: Mapping[str, Any]):
        from textual.app import App, ComposeResult

        class _ConfiguredApp(App[None]):
            CSS = ".pane { border: round $primary; padding: 1; margin: 1; } .pane-title { text-style: bold; }"
            TITLE = str((loader.document.get("pages", {}).get(page_id, {}) if isinstance(loader.document.get("pages"), Mapping) else {}).get("title", "Meta WebUI"))

            def compose(self) -> ComposeResult:
                yield from render_document(loader.document, page_id=page_id, scope=scope, source_resolver=loader.source_resolver)
        return _ConfiguredApp()
