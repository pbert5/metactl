import json
from pathlib import Path

import pytest

from operator_contract import (
    ContractError,
    contract_drift,
    ensure_mutation_compatible,
    load_snapshot,
)
from contract_snapshot import build_snapshot
from metactl_transport import HTTPTransport, TransportError


def contract_document():
    return {
        "version": "1.0.0",
        "revision": "operator-actions-1",
        "deployment_index": ["evolver.edge.status"],
        "actions": [{
            "id": "evolver.edge.status",
            "title": "Show status",
            "status": "implemented",
            "permissions": ["evolver:read"],
            "safety": {"risk": "low", "confirmation": "none", "reversible": True, "effect": "read"},
            "parameters": {},
        }],
        "api": {"evolver.edge.status": {"method": "GET", "path": "/api/evolver/controllers"}},
    }


def test_snapshot_records_server_revision_and_deterministic_provenance():
    snapshot = build_snapshot(contract_document(), source="evolver_server.control.contract", source_revision="abc123")
    assert snapshot["provenance"] == {
        "generator": "metactl.contract_snapshot",
        "source": "evolver_server.control.contract",
        "source_revision": "abc123",
    }
    assert snapshot["contract"]["revision"] == "operator-actions-1"
    assert json.dumps(snapshot, sort_keys=True) == json.dumps(
        build_snapshot(contract_document(), source="evolver_server.control.contract", source_revision="abc123"),
        sort_keys=True,
    )


def test_snapshot_loader_rejects_malformed_shape_fail_closed(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"contract": {"version": "1.0.0"}}), encoding="utf-8")
    with pytest.raises(ContractError, match="snapshot"):
        load_snapshot(path)


def test_live_contract_drift_checks_revision_and_action_metadata():
    local = contract_document()
    live = {"version": "1.0.0", "revision": "operator-actions-1", "actions": [{
        "id": "evolver.edge.status", "status": "implemented", "callable": True,
        "method": "GET", "path": "/api/evolver/controllers", "permissions": ["evolver:read"],
        "safety": local["actions"][0]["safety"],
    }]}
    assert contract_drift(local, live) == "clean"
    assert contract_drift(local, {**live, "revision": "operator-actions-2"}) == "incompatible"
    assert contract_drift(local, {**live, "actions": []}) == "changed"


def test_mutation_requires_compatible_live_contract_before_dispatch():
    with pytest.raises(ContractError, match="incompatible"):
        ensure_mutation_compatible(contract_document(), {"version": "1.0.0", "revision": "old", "actions": []})


def test_http_mutation_fails_closed_before_sender_on_live_drift():
    calls = []

    def sender(url, method, body, headers, timeout):
        calls.append((url, method))
        if url.endswith("/api/actions"):
            return 200, json.dumps({"version": "1.0.0", "revision": "old", "actions": []}).encode()
        return 200, b"{}"

    client = HTTPTransport(base_url="http://central.test", sender=sender)
    with pytest.raises(TransportError, match="incompatible"):
        client.action("evolver.controllers.archive", {"controller_id": "edge-a"})
    assert calls == [("http://central.test/api/actions", "GET")]


def test_runtime_modules_do_not_import_retired_generic_composition():
    root = Path(__file__).parents[1]
    runtime = [root / "cli.py", root / "metactl_transport.py", root / "doctor.py",
               root / "operator_tui/app.py", root / "api_workbench/adapters.py"]
    for path in runtime:
        text = path.read_text(encoding="utf-8")
        assert "framework" not in text
        assert "meta_webui_ui_runtime_textual" not in text


def test_snapshot_and_presentation_are_package_data():
    root = Path(__file__).parents[1]
    assert (root / "data/operator_contract_snapshot.json").is_file()
    assert (root / "data/presentation.json").is_file()
