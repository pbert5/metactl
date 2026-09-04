from __future__ import annotations

from pathlib import Path
import re

import pytest

from framework.action_catalog import ActionCatalogError, load_action_catalog, parse_action_catalog
from metactl_transport import HTTPTransport, TransportError, operator_action_ids


CATALOG = Path(__file__).parents[1] / "applications/evolver/actions.json"


def _catalog():
    return load_action_catalog(CATALOG)


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
