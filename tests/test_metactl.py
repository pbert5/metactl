from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from framework.action_catalog import load_action_catalog


ROOT = Path(__file__).parents[1]


def _metactl_module():
    spec = importlib.util.spec_from_file_location("metactl_entrypoint", ROOT / "tools" / "metactl.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_catalog_references_are_explicit_and_valid():
    index = json.loads((ROOT / "applications/deployment/action-catalog.json").read_text())
    assert index["deployment_index"] == [reference["id"] for reference in index["catalogs"]]
    for reference in index["catalogs"]:
        catalog = load_action_catalog(ROOT / "applications/deployment" / reference["path"])
        assert catalog.actions


def test_metactl_read_bindings_use_injected_central_transport(capsys):
    module = _metactl_module()
    class Fake:
        def __init__(self): self.calls = []
        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return [{"controller_id": "central-a"}]
    fake = Fake()
    assert module.main(["--json", "evolver.edge.controllers"], transport=fake) == 0
    assert json.loads(capsys.readouterr().out)["result"] == [{"controller_id": "central-a"}]
    assert fake.calls == [("evolver.edge.controllers", {})]


def test_metactl_planned_actions_never_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVOLVER_STATE_ROOT", str(tmp_path))
    module = _metactl_module()
    assert module.main(["--json", "evolver.run.start", "--run-id", "r", "--bundle-id", "b"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "planned"
    assert result["reason"] == "unavailable"


def test_human_aliases_preserve_action_ids_and_redact_projection(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"controller": {"controller_id": parameters.get("controller_id"), "credential": "secret"}}

    fake = Fake()
    assert module.main(["--json", "controllers", "show", "central-a"], transport=fake) == 0
    result = json.loads(capsys.readouterr().out)
    assert fake.calls == [("evolver.controllers.show", {"controller_id": "central-a"})]
    assert result["result"]["controller"]["credential"] == "<redacted>"


def test_adopt_is_explicitly_forced_and_confirmation_preserving(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self): self.calls = []
        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"accepted": True}

    fake = Fake()
    assert module.main(["--json", "controllers", "adopt", "https://edge", "--yes"], transport=fake) == 0
    json.loads(capsys.readouterr().out)
    assert fake.calls == [("evolver.controllers.add", {"server_url": "https://edge", "purpose": "forced_adoption", "ttl_seconds": 900})]


def test_explicit_enrollment_action_emits_one_time_token_but_projections_redact(capsys):
    module = _metactl_module()

    class Fake:
        def action(self, action_id, parameters):
            return {"enrollment_token": "one-time", "credential": "durable-secret"}

    assert module.main(["--json", "controllers", "add", "https://edge", "--yes"], transport=Fake()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"]["enrollment_token"] == "one-time"
    assert result["result"]["credential"] == "<redacted>"


def test_commands_watch_polls_show_projection_and_distinguishes_actuation(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []
            self.results = [{"command": {"disposition": "queued"}}, {"command": {"disposition": "completed"}}]

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return self.results.pop(0)

    fake = Fake()
    assert module.main(["--json", "controllers", "commands", "watch", "central-a", "cmd-1", "--timeout", "1", "--interval", ".001"], transport=fake) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed"
    assert result["result"]["disposition"] == "completed"
    assert result["result"]["physical_actuation_verified"] is False
    assert len(fake.calls) == 2


def test_central_http_transport_binds_route_headers_and_body():
    module = _metactl_module()
    from tools.metactl_transport import HTTPTransport
    seen = {}
    def sender(url, method, body, headers, timeout):
        seen.update(url=url, method=method, body=body, headers=dict(headers), timeout=timeout)
        return 200, b'{"accepted":true}'
    client = HTTPTransport(base_url="http://central.test/", sender=sender,
                           operator="alice", token="secret", shared_secret="proxy",
                           permissions="evolver:runs:write")
    assert client.action("evolver.run.pause", {"run_id": "run-a"}) == {"accepted": True}
    assert seen["url"] == "http://central.test/api/evolver/runs/run-a/commands"
    assert seen["method"] == "POST"
    assert seen["body"] == {"action": "pause", "run_id": "run-a"}
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["headers"]["X-Meta-Webui-Evolver-Operator"] == "alice"
    assert seen["headers"]["X-Meta-Webui-Evolver-Control-Secret"] == "proxy"


def test_metactl_release_build_presents_grouped_action_and_central_route(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"status": "built", "output": "/srv/releases/evolver"}

    fake = Fake()
    assert module.main(["--json", "releases", "build", "--version", "1.2.3", "--yes"], transport=fake) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"]["status"] == "built"
    assert fake.calls == [("evolver.release.build", {"output": "releases/evolver", "version": "1.2.3"})]


def test_central_http_transport_binds_release_build_route():
    from tools.metactl_transport import HTTPTransport
    seen = {}

    def sender(url, method, body, headers, timeout):
        seen.update(url=url, method=method, body=body, headers=dict(headers))
        return 200, b'{"status":"built"}'

    client = HTTPTransport(base_url="http://central.test", sender=sender, token="secret")
    assert client.action("evolver.release.build", {"version": "1.2.3"}) == {"status": "built"}
    assert seen["url"] == "http://central.test/api/evolver/releases/build"
    assert seen["method"] == "POST"
    assert seen["body"] == {"action": "build", "version": "1.2.3"}


@pytest.mark.parametrize("status,kind", [(401, "unauthorized"), (403, "forbidden"),
    (404, "not_found"), (409, "conflict"), (503, "central_failure")])
def test_central_http_transport_normalizes_http_failures(status, kind):
    from tools.metactl_transport import HTTPTransport, TransportError
    client = HTTPTransport(base_url="http://central.test", sender=lambda *args: (status, b'{"error":"detail"}'))
    with pytest.raises(TransportError) as raised:
        client.action("evolver.edge.controllers", {})
    assert raised.value.as_dict()["kind"] == kind
    assert raised.value.as_dict()["status"] == status


def test_central_http_transport_normalizes_malformed_and_network_failures():
    from tools.metactl_transport import HTTPTransport, TransportError
    for sender, kind in ((lambda *args: (200, b"nope"), "malformed_response"),
                         (lambda *args: (_ for _ in ()).throw(OSError("offline")), "network_failure")):
        with pytest.raises(TransportError) as raised:
            HTTPTransport(base_url="http://central.test", sender=sender).action("evolver.edge.controllers", {})
        assert raised.value.as_dict()["kind"] == kind
