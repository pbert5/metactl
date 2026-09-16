"""Read-only central target and action-catalog diagnostics."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

try:
    from .metactl_transport import OperatorTarget, TransportError, configured_transport, resolve_operator_target
    from .presentation import safe_target_url
except ImportError:
    from metactl_transport import OperatorTarget, TransportError, configured_transport, resolve_operator_target
    from presentation import safe_target_url


CATALOG = Path(__file__).with_name("applications") / "evolver" / "actions.json"
def _read_catalog(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def doctor_report(*, transport: Any = None, target: OperatorTarget | None = None,
                  local_catalog: Mapping[str, Any] | None = None) -> dict[str, Any]:
    target = target or resolve_operator_target()
    report: dict[str, Any] = {
        "target": {"url": safe_target_url(target.url), "source": target.source},
        "reachable": False,
        "auth": {name: bool(target.auth.get(name, False)) for name in ("operator", "token", "shared_secret")},
        "discovery": {"status": "unavailable"},
        "drift": {"status": "not_checked"},
        "remediation": [],
    }
    client = transport or configured_transport()
    try:
        discovered = client.discover_actions()
        report["reachable"] = True
        actions = discovered.get("actions", [])
        if not isinstance(actions, list):
            raise TransportError("malformed_response", "action discovery was malformed")
        report["discovery"] = {"status": "ok", "version": discovered.get("version"), "actions": len(actions)}
    except Exception:
        # A doctor probe must never turn an implementation detail into output;
        # only successful /api/actions discovery establishes reachability.
        report["discovery"] = {"status": "unavailable"}
        report["remediation"] = ["rtk tools/dev-env server network", "rtk tools/dev-env server exec metactl doctor"]
        return report

    if local_catalog is None:
        try:
            local_catalog = _read_catalog(CATALOG)
        except (OSError, ValueError, json.JSONDecodeError):
            local_catalog = None
    if local_catalog is None:
        report["drift"] = {"status": "not_available"}
        return report
    local_ids = set(local_catalog.get("api", {}))
    live_ids = {item.get("id") for item in actions if isinstance(item, Mapping)}
    report["drift"] = {"status": "clean" if local_ids == live_ids and local_catalog.get("version") == discovered.get("version") else "changed",
                        "local_actions": len(local_ids), "live_actions": len(live_ids)}
    return report
