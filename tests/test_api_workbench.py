from dataclasses import replace
import asyncio
import json
from pathlib import Path

import pytest

from api_workbench.adapters import repository_registry, catalog_registry, openapi_registry, audit_routes, live_registry
from api_workbench.model import Endpoint, EndpointRegistry, WorkbenchError, compare
from api_workbench.execution import Policy, Session, FixtureClient, HTTPClient, prepare, export_request, coerce
from api_workbench.evidence import resolve_tests, command_for


ROOT = Path(__file__).resolve().parents[1]


def read_endpoint(**changes):
    return replace(Endpoint("demo.get", "demo", "GET", "/api/items/{id}",
        parameters={"id": {"in": "path", "type": "string", "required": True},
                    "limit": {"in": "query", "type": "integer", "default": 5}}, status="implemented"), **changes)


def test_real_deployment_keeps_unmapped_actions_and_route_aliases():
    registry = repository_registry(ROOT)
    assert registry.endpoints["core.access.status"].method is None
    aliases = [e for e in registry.endpoints.values() if e.method == "GET" and e.path == "/api/evolver/controllers"]
    assert len(aliases) > 1
    assert not compare(registry, registry)


def test_real_catalog_request_matches_existing_transport():
    e = repository_registry(ROOT).endpoints["evolver.runs.pause"]
    values = {n: "r/1" if s["type"] == "string" else 1 for n, s in e.parameters.items() if s.get("required")}
    path, body, _ = prepare(e, values)
    assert "%2F" in path
    assert body["action"] == "pause"
    assert "expected_revision" in body
    assert "run_id" not in body


def test_parameter_validation_prevents_network_and_encodes_values():
    endpoint = read_endpoint()
    assert prepare(endpoint, {"id": "a/b ?"})[0] == "/api/items/a%2Fb%20%3F?limit=5"
    with pytest.raises(WorkbenchError, match="required"):
        prepare(endpoint, {})
    with pytest.raises(WorkbenchError):
        prepare(endpoint, {"id": "a", "limit": "true"})
    with pytest.raises(WorkbenchError, match="undeclared"):
        prepare(endpoint, {"id": "a", "other": 1})


@pytest.mark.parametrize("raw,spec,expected", [("false", {"type": "boolean"}, False), ("3", {"type": "integer"}, 3),
    ('[1,2]', {"type": "array"}, [1, 2]), ('{"a":1}', {"type": "object"}, {"a": 1})])
def test_coerce(raw, spec, expected):
    assert coerce(raw, spec) == expected


@pytest.mark.parametrize("method,effect", [("GET", "hardware"), ("POST", "mutation"), ("DELETE", "destructive"), ("POST", "unknown")])
def test_mutations_fail_closed(method, effect):
    endpoint = read_endpoint(method=method, safety={"effect": effect} if effect != "unknown" else {})
    with pytest.raises(WorkbenchError):
        Policy().check(endpoint, fixture=False, confirmation=endpoint.id)
    with pytest.raises(WorkbenchError):
        Policy(True, True).check(endpoint, fixture=False, confirmation="wrong")
    Policy(True, True).check(endpoint, fixture=False, confirmation=endpoint.id)


def test_physical_legacy_and_unknown_writes_require_hardware_gate():
    endpoint = read_endpoint(method="POST", safety={"risk": "low", "confirmation": "none"})
    assert endpoint.badge == "HW?"
    with pytest.raises(WorkbenchError, match="hardware"):
        Policy(True).check(endpoint, fixture=False, confirmation=endpoint.id)


def test_explicit_read_cannot_override_physical_confirmation():
    assert read_endpoint(safety={"effect": "read", "confirmation": "physical"}).effect == "hardware"


def test_observed_drift_blocks_execution_before_network():
    session = Session(FixtureClient([]), blocked=["demo.get"])
    with pytest.raises(WorkbenchError, match="drift"):
        session.execute(read_endpoint(), {"id": "a"})


def test_fixture_never_falls_back_and_records_expected_error_status():
    client = FixtureClient([{"method": "GET", "path": "/api/items/a?limit=5", "status": 409, "body": {"error": "stale"}}])
    session = Session(client)
    result = session.execute(read_endpoint(), {"id": "a"}, expected_status=409)
    assert result.status == 409
    assert all(c["passed"] is not False for c in result.checks)
    assert session.execute(read_endpoint(), {"id": "missing"}).error


def test_redaction_covers_history_exports_and_echoed_credentials():
    secret = "one-time-sensitive-value"
    client = FixtureClient([{"method": "GET", "path": "/api/items/a?limit=5", "status": 200,
        "headers": {"Set-Cookie": secret}, "body": {"token": secret, "echo": secret}}])
    result = Session(client).execute(read_endpoint(), {"id": "a"}, headers={"Authorization": secret})
    assert secret not in str(result)
    assert secret not in export_request(result)
    assert secret not in export_request(result, "python")


def test_non_json_is_not_persisted_and_history_is_bounded():
    session = Session(FixtureClient([{"method": "GET", "path": "/api/items/a?limit=5", "status": 200, "raw": "secret-unstructured"}]), history_limit=2)
    for _ in range(3):
        result = session.execute(read_endpoint(), {"id": "a"})
    assert len(session.history) == 2
    assert "secret-unstructured" not in result.raw


def test_drift_compares_fields_not_just_routes():
    endpoint = read_endpoint()
    a, b = EndpointRegistry(), EndpointRegistry()
    a.add(endpoint)
    b.add(replace(endpoint, permissions=("operate",)))
    assert compare(a, b)[0]["fields"] == ["permissions"]
    b.add(replace(endpoint, id="demo.alias"))
    assert any(r["kind"] == "live_only" for r in compare(a, b))


def test_reference_escape_is_rejected(tmp_path):
    apps = tmp_path / "applications/deployment"
    apps.mkdir(parents=True)
    (apps / "action-catalog.json").write_text(json.dumps({"deployment_index": ["escape"], "catalogs": [{"id": "escape", "path": "../../../secret"}]}))
    with pytest.raises(WorkbenchError, match="escapes"):
        repository_registry(tmp_path)


def openapi_document():
    return {"openapi": "3.0.3", "info": {"title": "Test", "version": "1"},
        "components": {"schemas": {"Result": {"type": "object", "required": ["value"], "properties": {"value": {"type": "integer"}}}}},
        "paths": {"/items/{id}": {"parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
            "get": {"operationId": "items.get", "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Result"}}}}}}}}}


def test_openapi_schema_refs_and_response_checks():
    endpoint = openapi_registry(openapi_document(), "fixture").endpoints["items.get"]
    session = Session(FixtureClient([{"method": "GET", "path": "/items/a", "status": 200, "body": {"wrong": 1}}]))
    result = session.execute(endpoint, {"id": "a"})
    assert any(c["check"] == "response schema" and c["passed"] is False for c in result.checks)


def test_openapi_external_refs_fail_without_fetching():
    document = openapi_document()
    document["components"]["schemas"]["Result"] = {"$ref": "https://example.invalid/secrets"}
    with pytest.raises(WorkbenchError, match="external"):
        openapi_registry(document, "fixture")


def test_live_catalog_never_grants_executable_evidence():
    local = json.loads((ROOT / "applications/evolver/actions.json").read_text())
    class Client:
        base_url = "http://test"
        def read(self, path):
            assert path == "/api/meta/actions"
            return 200, {}, json.dumps({"format": "meta-api-catalog/1", "catalogs": [{"id": "evolver", "catalog": local}]}).encode()
    endpoint = next(iter(live_registry(Client()).endpoints.values()))
    with pytest.raises(WorkbenchError, match="local repository"):
        resolve_tests(endpoint)


def test_local_evidence_is_exact_and_missing_is_not_guessed():
    registry = repository_registry(ROOT)
    endpoint = registry.endpoints["evolver.edge.status"]
    targets = resolve_tests(endpoint)
    assert len(targets) == 1
    assert command_for(targets[0])[-1].endswith("::test_central_http_transport_binds_route_headers_and_body")
    with pytest.raises(WorkbenchError):
        resolve_tests(replace(endpoint, evidence={"tests": ["../outside.py"]}))


def test_source_audit_never_imports_code(tmp_path):
    (tmp_path / "app.py").write_text('raise RuntimeError("must not execute")\n@app.get("/extra")\ndef endpoint(): pass\n')
    result = audit_routes(tmp_path, EndpointRegistry())
    assert result["routes"][0]["undeclared"]


def test_http_client_rejects_credential_urls_and_redirects():
    with pytest.raises(WorkbenchError):
        HTTPClient("http://user:password@example.com")
    from api_workbench.execution import NoRedirect
    assert NoRedirect().redirect_request(None, None, 302, "", {}, "http://other") is None


def test_catalog_body_cannot_override_path_or_action():
    endpoint = repository_registry(ROOT).endpoints["evolver.runs.pause"]
    with pytest.raises(WorkbenchError):
        prepare(endpoint, {"run_id": "R", "expected_revision": 1}, {"run_id": "other"})
    with pytest.raises(WorkbenchError):
        prepare(endpoint, {"run_id": "R", "expected_revision": 1}, {"action": "resume"})


def test_tui_search_select_send_history_and_export():
    pytest.importorskip("textual")
    from api_workbench.app import WorkbenchApp
    endpoint = read_endpoint(parameters={"id": {"in": "path", "type": "string", "required": True, "default": "a"}})
    registry = EndpointRegistry()
    registry.add(endpoint)
    app = WorkbenchApp(registry, Session(FixtureClient([{"method": "GET", "path": "/api/items/a", "status": 200, "body": {"value": 1}}])))
    async def scenario():
        async with app.run_test(size=(160, 54)) as pilot:
            tree = app.query_one("#endpoint-tree")
            leaf = tree.root.children[0].children[0].children[0]
            tree.select_node(leaf)
            await pilot.pause()
            app.action_send()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(app.session.history) == 1
            assert app.current_exchange.status == 200
            app.action_curl()
            assert "curl" in app.query_one("#export").text
            app.query_one("#search").value = "absent"
            await pilot.pause()
            assert not tree.root.children
    asyncio.run(scenario())
