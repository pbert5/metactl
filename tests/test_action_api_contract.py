from __future__ import annotations

from pathlib import Path
import json
import re

import pytest

from framework.action_catalog import ActionCatalogError, load_action_catalog, parse_action_catalog
from metactl_transport import HTTPTransport, TransportError, _headers, operator_action_ids


CATALOG = Path(__file__).parents[1] / "applications/evolver/actions.json"


def _catalog():
    return load_action_catalog(CATALOG)


def _live_manifest():
    from operator_contract import build_manifest, load_snapshot
    return json.dumps(build_manifest(load_snapshot())).encode()


def test_enrollment_catalog_matches_endpoint_contract_and_preserves_controls():
    action = _catalog().action("evolver.controllers.add")
    assert action is not None
    assert action["parameters"] == {
        "server_url": {"type": "string"},
        "endpoint_id": {"type": "string"},
        "ttl_seconds": {"type": "integer", "default": 900},
        "purpose": {"type": "string", "default": "enrollment"},
        "release_binding": {"type": "object"},
    }
    assert action["safety"] == {"risk": "medium", "confirmation": "required", "reversible": True}
    assert action["permissions"] == ["manage_controller"]


def test_safe_stop_catalog_is_dedicated_physical_no_lease_contract():
    catalog = _catalog()
    action = catalog.action("evolver.controllers.safe_stop")
    assert action is not None
    assert catalog.api["evolver.controllers.safe_stop"] == {
        "method": "POST",
        "path": "/api/evolver/controllers/{controller_id}/safe-stop",
    }
    assert action["permissions"] == ["operate_run"]
    assert action["safety"]["effect"] == "hardware"
    assert action["safety"]["confirmation"] == "physical"
    assert action["parameters"] == {
        "controller_id": {"type": "string", "required": True},
        "idempotency_key": {"type": "string"},
    }
    assert not any(name in action["parameters"] for name in ("target", "channel", "level", "duration", "lease_token"))


def test_calibration_catalog_freezes_central_routes_permissions_and_od_boundary():
    catalog = _catalog()
    expected = {
        "evolver.calibrations.list": ("GET", "/api/evolver/calibrations"),
        "evolver.calibrations.show": ("GET", "/api/evolver/calibrations/{calibration_id}"),
        "evolver.calibrations.sessions.create": ("POST", "/api/evolver/calibrations/sessions"),
        "evolver.calibrations.sessions.observation": ("POST", "/api/evolver/calibrations/sessions/{session_id}/observations"),
        "evolver.calibrations.sessions.fit": ("POST", "/api/evolver/calibrations/sessions/{session_id}/fit"),
        "evolver.calibrations.sessions.accept": ("POST", "/api/evolver/calibrations/sessions/{session_id}/accept"),
        "evolver.calibrations.sessions.cancel": ("POST", "/api/evolver/calibrations/sessions/{session_id}/cancel"),
        "evolver.calibrations.sessions.capture": ("POST", "/api/evolver/calibrations/sessions/{session_id}/capture"),
        "evolver.calibrations.artifacts.deliver": ("POST", "/api/evolver/calibrations/artifacts/{artifact_id}/deliver"),
        "evolver.calibrations.artifacts.supersede": ("POST", "/api/evolver/calibrations/artifacts/{artifact_id}/supersede"),
        "evolver.calibrations.artifacts.invalidate": ("POST", "/api/evolver/calibrations/artifacts/{artifact_id}/invalidate"),
    }
    assert {action["id"] for action in catalog.actions if action["id"].startswith("evolver.calibrations.")} == set(expected)
    assert {key: (value["method"], value["path"]) for key, value in catalog.api.items() if key in expected} == expected
    for action_id, (method, _) in expected.items():
        action = catalog.action(action_id)
        assert action is not None
        if method == "GET":
            assert action["permissions"] == ["evolver:read"]
            assert action["safety"]["effect"] == "read"
        else:
            assert action["permissions"] == ["manage_calibration"]
            assert action["safety"]["effect"] == "mutation"
    assert not any("od" in action_id and action_id.endswith(("fit", "accept")) for action_id in catalog.api)


def test_operator_routes_are_catalog_owned_and_complete():
    catalog = load_action_catalog(CATALOG)
    assert operator_action_ids() == frozenset(catalog.api)
    assert "ROUTE_BINDINGS" not in __import__("metactl_transport").__dict__
    for action_id, contract in catalog.api.items():
        assert contract["method"] in {"GET", "POST", "PATCH", "DELETE"}
        assert contract["path"].startswith("/api/")
        for name in __import__("re").findall(r"\{([^{}]+)\}", contract["path"]):
            assert catalog.action(action_id)["parameters"][name]["required"] is True


def test_catalog_rejects_invalid_api_path_parameter():
    document = load_action_catalog(CATALOG).as_dict()
    document["api"]["evolver.controllers.show"]["path"] = "/api/evolver/controllers/{missing}"
    with pytest.raises(ActionCatalogError, match="not declared"):
        parse_action_catalog(document)


def test_transport_expands_and_encodes_catalog_route():
    seen = {}
    def sender(url, method, body, headers, timeout):
        seen.update(url=url, method=method)
        return 200, b"{}"
    HTTPTransport(base_url="http://central", sender=sender).action("evolver.controllers.show", {"controller_id": "a/b"})
    assert seen == {"url": "http://central/api/evolver/controllers/a%2Fb", "method": "GET"}


def test_enrollment_transport_forwards_endpoint_and_release_binding_fields():
    seen = {}

    def sender(url, method, body, headers, timeout):
        if url.endswith("/api/actions"):
            return 200, _live_manifest()
        seen.update(url=url, method=method, body=body)
        return 201, b"{}"

    HTTPTransport(base_url="http://central", sender=sender).action(
        "evolver.controllers.add",
        {"endpoint_id": "central", "release_binding": {
            "release": "r1", "source_revision": "abcdef1", "manifest_sha256": "a" * 64,
        }},
    )
    assert seen == {
        "url": "http://central/api/evolver/enrollment-tokens",
        "method": "POST",
        "body": {"action": "add", "endpoint_id": "central", "release_binding": {
            "release": "r1", "source_revision": "abcdef1", "manifest_sha256": "a" * 64,
        }},
    }


def test_calibration_observation_transport_keeps_action_decoration_outside_observation_shape():
    seen = {}

    def sender(url, method, body, headers, timeout):
        if url.endswith("/api/actions"):
            return 200, _live_manifest()
        seen.update(url=url, method=method, body=body)
        return 200, b"{}"

    HTTPTransport(base_url="http://central", sender=sender).action(
        "evolver.calibrations.sessions.observation",
        {"session_id": "session/a", "raw_value": 1432, "reference_value": 25.0},
    )
    assert seen == {
        "url": "http://central/api/evolver/calibrations/sessions/session%2Fa/observations",
        "method": "POST",
        "body": {"action": "observation", "session_id": "session/a", "raw_value": 1432, "reference_value": 25.0},
    }
    assert "action" not in _catalog().action("evolver.calibrations.sessions.observation")["parameters"]


@pytest.mark.parametrize("base_url", [
    "https://user:password@central.test",
    "https://central.test/api?token=secret",
    "http:///missing-authority",
])
def test_transport_rejects_secret_bearing_or_malformed_base_urls(base_url):
    called = False

    def sender(*args):
        nonlocal called
        called = True
        return 200, b"{}"

    with pytest.raises(TransportError) as raised:
        HTTPTransport(base_url=base_url, sender=sender)
    assert raised.value.kind == "malformed_target"
    assert called is False


def test_bearer_mode_does_not_forward_proxy_operator_credentials():
    headers = _headers(operator="alice", token="human-token", shared_secret="proxy-secret",
                       permissions="operate_run")

    assert headers["Authorization"] == "Bearer human-token"
    assert "X-Meta-Webui-Evolver-Operator" not in headers
    assert "X-Meta-Webui-Evolver-Control-Secret" not in headers
    assert "X-Meta-Webui-Evolver-Permissions" not in headers


def test_transport_maps_optional_get_parameters_to_query_string():
    seen = {}
    def sender(url, method, body, headers, timeout):
        seen.update(url=url, method=method, body=body)
        return 200, b"{}"
    HTTPTransport(base_url="http://central", sender=sender).action(
        "evolver.runs.list", {"controller_id": "edge/a", "state": "paused", "limit": 10})
    assert seen == {
        "url": "http://central/api/evolver/runs?controller_id=edge%2Fa&state=paused&limit=10",
        "method": "GET", "body": None,
    }


def _missing_route_parameters():
    for action_id, contract in sorted(_catalog().api.items()):
        for name in re.findall(r"\{([^{}]+)\}", contract["path"]):
            yield pytest.param(action_id, name, id=f"{action_id}:{name}")


@pytest.mark.parametrize("action_id,missing", list(_missing_route_parameters()))
def test_transport_rejects_every_catalog_route_with_each_missing_path_parameter(action_id, missing):
    catalog = _catalog()
    placeholders = re.findall(r"\{([^{}]+)\}", catalog.api[action_id]["path"])

    called = False

    def sender(*args):
        nonlocal called
        called = True
        return 200, b"{}"

    parameters = {name: "value" for name in placeholders if name != missing}
    with pytest.raises(TransportError, match="missing route parameter") as raised:
        HTTPTransport(base_url="http://central", sender=sender).action(action_id, parameters)
    assert raised.value.kind == "bad_request"
    assert called is False


@pytest.mark.parametrize("action_id", sorted(set(_catalog().deployment_index) - set(_catalog().api)))
def test_transport_rejects_every_catalog_action_without_an_operator_api(action_id):
    called = False

    def sender(*args):
        nonlocal called
        called = True
        return 200, b"{}"

    with pytest.raises(TransportError) as raised:
        HTTPTransport(base_url="http://central", sender=sender).action(action_id, {})
    assert raised.value.kind == "unknown_action"
    assert called is False
