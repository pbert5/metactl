"""Shared operator presentation and safe display helpers."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_URL_PARTS = ("credential", "password", "secret", "token", "private_key", "api_key", "authorization")


def safe_target_url(value: str) -> str:
    """Return a display-safe HTTP URL without userinfo or query secrets."""
    try:
        parts = urlsplit(value)
        is_http = parts.scheme.lower() in {"http", "https"}
        if not (is_http or value.startswith("//")):
            return value
        if not parts.netloc:
            return "<redacted URL>"
        hostname = parts.hostname or ""
        port = f":{parts.port}" if parts.port is not None else ""
        query = urlencode([
            (key, "<redacted>" if any(part in key.lower() for part in SENSITIVE_URL_PARTS) else item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, hostname + port, parts.path, query, ""))
    except (ValueError, UnicodeError):
        return "<redacted URL>"


def redact(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact secret-shaped fields and embedded target URLs."""
    if key is not None and any(part in key.lower() for part in SENSITIVE_URL_PARTS):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {name: redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str) and value.startswith(("http://", "https://", "//")):
        return safe_target_url(value)
    return value


def load_presentation(path: Path) -> Mapping[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return document if isinstance(document, Mapping) else {}


def presentation_paths(presentation: Mapping[str, Any]) -> dict[str, str]:
    """Map catalog action IDs to their first human-facing CLI path."""
    result: dict[str, str] = {}
    groups = presentation.get("groups", presentation)

    def visit(tree: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> None:
        for name, value in tree.items():
            if name in {"description", "positionals", "aliases", "defaults", "action_id"}:
                continue
            action_id = value if isinstance(value, str) else value.get("action_id") if isinstance(value, Mapping) else None
            if isinstance(action_id, str):
                result.setdefault(action_id, "metactl " + " ".join((*prefix, name)))
            elif isinstance(value, Mapping):
                children = {key: child for key, child in value.items()
                            if key not in {"description", "positionals", "aliases", "defaults", "action_id"}}
                visit(children, (*prefix, name))

    if isinstance(groups, Mapping):
        visit(groups)
    return result
