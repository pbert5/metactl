from __future__ import annotations

from pathlib import Path

import pytest

from framework.action_catalog import ActionCatalogError, load_action_catalog, parse_action_catalog
from metactl_transport import HTTPTransport, operator_action_ids


CATALOG = Path(__file__).parents[1] / "applications/evolver/actions.json"


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
