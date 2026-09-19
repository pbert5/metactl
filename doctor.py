"""Read-only central target and action-catalog diagnostics."""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

try:
    from .metactl_transport import OperatorTarget, TransportError, configured_transport, resolve_operator_target
    from .operator_contract import ContractError, load_snapshot
    from .presentation import safe_target_url
except ImportError:
    from metactl_transport import OperatorTarget, TransportError, configured_transport, resolve_operator_target
    from operator_contract import ContractError, load_snapshot
    from presentation import safe_target_url


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
    try:
        client = transport or configured_transport()
    except TransportError as error:
        discovery_status = "unavailable" if error.kind == "network_failure" else "error"
        report["discovery"] = {"status": discovery_status, "kind": error.kind, **(
            {"status_code": error.status} if error.status is not None else {})}
        report["remediation"] = _remediation(error.kind)
        return report
    try:
        discovered = client.discover_actions()
        report["reachable"] = True
        actions = discovered.get("actions", [])
        if not isinstance(actions, list):
            raise TransportError("malformed_response", "action discovery was malformed")
        report["discovery"] = {"status": "ok", "version": discovered.get("version"), "actions": len(actions)}
    except TransportError as error:
        discovery_status = "unavailable" if error.kind == "network_failure" else "error"
        report["discovery"] = {"status": discovery_status, "kind": error.kind, **(
            {"status_code": error.status} if error.status is not None else {})}
        report["remediation"] = _remediation(error.kind)
        return report
    except OSError:
        report["discovery"] = {"status": "unavailable", "kind": "network_failure"}
        report["remediation"] = _remediation("network_failure")
        return report

    if local_catalog is None:
        try:
            local_catalog = load_snapshot()
        except ContractError:
            local_catalog = None
    if local_catalog is None:
        report["drift"] = {"status": "not_available"}
        return report
    local_ids = set(local_catalog.get("api", {}))
    live_ids = {item.get("id") for item in actions if isinstance(item, Mapping)}
    report["drift"] = {"status": "clean" if local_ids == live_ids and local_catalog.get("version") == discovered.get("version") and local_catalog.get("revision") == discovered.get("revision") else "changed",
                        "local_actions": len(local_ids), "live_actions": len(live_ids)}
    return report


def _remediation(kind: str) -> list[str]:
    if kind == "unauthorized":
        return ["set META_WEBUI_METACTL_TOKEN for the WebUI gateway", "verify the token is current"]
    if kind == "forbidden":
        return ["use an operator identity permitted by the WebUI gateway", "ask an administrator to grant the required permission"]
    if kind == "malformed_target":
        return ["set META_WEBUI_METACTL_CENTRAL_URL to an HTTP(S) URL without credentials, query parameters, or fragments"]
    if kind == "malformed_response":
        return ["verify the WebUI gateway exposes /api/actions", "check that the gateway response is valid JSON"]
    return ["rtk tools/dev-env server network", "rtk tools/dev-env server exec metactl doctor"]
