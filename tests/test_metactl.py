from __future__ import annotations

import importlib.util
import ast
import io
import json
from pathlib import Path

import pytest

from framework.action_catalog import load_action_catalog


ROOT = Path(__file__).parents[1]
CATALOG = ROOT / "applications" / "evolver" / "actions.json"


def _evolver_catalog():
    return load_action_catalog(CATALOG)


def _cli_value(spec):
    return {"string": "value", "integer": "1", "number": "1.5", "boolean": "true",
            "object": "{}", "array": "[]", "json": "{}"}[spec.get("type", "string")]


def _required_arguments(action):
    arguments = []
    for name, spec in action["parameters"].items():
        if spec.get("required"):
            arguments.extend([f"--{name.replace('_', '-')}", _cli_value(spec)])
    return arguments


def _metactl_module():
    spec = importlib.util.spec_from_file_location("metactl_entrypoint", ROOT / "tools" / "metactl.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bare_metactl_is_a_successful_discovery_landing_page(capsys):
    module = _metactl_module()
    assert module.main([]) == 0
    output = capsys.readouterr().out
    assert "metactl actions list" in output
    assert "metactl interactive" in output
    assert "metactl tui                 # operator TUI" in output
    assert "metactl api check --repo ." in output
    assert "metactl api tui --repo .    # API Workbench" in output
    assert "metactl doctor" in output


def test_top_level_help_names_all_routed_entrypoints(capsys):
    module = _metactl_module()
    with pytest.raises(SystemExit) as raised:
        module.main(["--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "metactl doctor" in output
    assert "metactl tui (operator TUI)" in output
    assert "metactl api tui (API Workbench)" in output


def test_top_level_tui_alias_exposes_operator_help(capsys):
    module = _metactl_module()
    with pytest.raises(SystemExit) as raised:
        module.main(["tui", "--help"])
    assert raised.value.code == 0
    assert "usage: metactl tui" in capsys.readouterr().out


def test_api_tui_alias_keeps_api_workbench_help(capsys):
    module = _metactl_module()
    with pytest.raises(SystemExit) as raised:
        module.main(["api", "tui", "--help"])
    assert raised.value.code == 0
    assert "usage: metactl api" in capsys.readouterr().out


def test_public_doctor_json_dispatches_read_only_report(capsys):
    module = _metactl_module()

    class Fake:
        def discover_actions(self):
            return {"version": "2", "actions": [{"id": "evolver.edge.status"}]}

    assert module.main(["doctor", "--format", "json"], transport=Fake()) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["reachable"] is True
    assert report["discovery"] == {"status": "ok", "version": "2", "actions": 1}


def test_controllers_help_is_a_real_nested_parser_path(capsys):
    module = _metactl_module()
    with pytest.raises(SystemExit) as raised:
        module.main(["controllers", "--help"], transport=object())
    assert raised.value.code == 0
    text = capsys.readouterr().out
    assert "show" in text
    assert "freshness" in text
    assert "recovery" in text


def test_nested_human_path_and_raw_action_id_dispatch_same_action(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"ok": True}

    fake = Fake()
    assert module.main(["controllers", "show", "edge-a", "--json"], transport=fake) == 0
    capsys.readouterr()
    human_call = fake.calls[-1]
    assert module.main(["evolver.controllers.show", "--controller-id", "edge-a", "--json"], transport=fake) == 0
    capsys.readouterr()
    assert fake.calls[-1] == human_call


def test_commands_group_uses_each_action_contract_for_positionals(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"ok": True}

    fake = Fake()
    assert module.main(["controllers", "commands", "list", "edge-a", "--json"], transport=fake) == 0
    capsys.readouterr()
    assert fake.calls[-1] == ("evolver.controllers.commands.list", {"controller_id": "edge-a"})

    assert module.main(["controllers", "commands", "show", "edge-a", "command-1", "--json"], transport=fake) == 0
    capsys.readouterr()
    assert fake.calls[-1] == ("evolver.controllers.commands.show", {
        "controller_id": "edge-a", "command_id": "command-1",
    })


def test_compatibility_alias_is_preserved_by_presentation_model(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"ok": True}

    fake = Fake()
    assert module.main(["controllers", "adopt", "https://edge", "--yes", "--json"], transport=fake) == 0
    capsys.readouterr()
    assert fake.calls[-1][0] == "evolver.controllers.add"


def test_planned_human_path_is_visible_but_never_dispatched(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"unexpected": True}

    fake = Fake()
    assert module.main(["experiments", "enqueue", "--definition", "{}", "--json"], transport=fake) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "unavailable"
    assert result["action"] == "evolver.experiments.enqueue"
    assert fake.calls == []


def test_json_output_contains_only_one_machine_readable_document(capsys):
    module = _metactl_module()

    class Fake:
        def action(self, action_id, parameters):
            return {"action_id": action_id, "parameters": parameters}

    assert module.main(["controllers", "list", "--json"], transport=Fake()) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out)["action"] == "evolver.controllers.list"


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


def test_metactl_unavailable_actions_never_dispatch(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EVOLVER_STATE_ROOT", str(tmp_path))
    module = _metactl_module()
    assert module.main(["--json", "evolver.run.start", "--run-id", "r", "--bundle-id", "b"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == _evolver_catalog().action("evolver.run.start")["status"]
    assert result["reason"] == "unavailable"


@pytest.mark.parametrize("action_id", [
    action["id"] for action in load_action_catalog(CATALOG).actions
    if action["status"] != "implemented"
])
def test_every_non_implemented_catalog_action_is_cli_negative_and_never_dispatches(capsys, action_id):
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"unexpected": True}

    fake = Fake()
    action = _evolver_catalog().action(action_id)
    assert action is not None
    assert module.main(["--json", action_id, *_required_arguments(action)], transport=fake) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"action": action_id, "parameters": {
        name: json.loads(_cli_value(spec)) if spec["type"] in {"json", "object", "array"}
        else int(_cli_value(spec)) if spec["type"] == "integer"
        else float(_cli_value(spec)) if spec["type"] == "number"
        else _cli_value(spec)
        for name, spec in action["parameters"].items() if spec.get("required")
    }, "reason": "unavailable", "status": action["status"]}
    assert fake.calls == []


@pytest.mark.parametrize("action_id", [action["id"] for action in load_action_catalog(CATALOG).actions])
def test_every_catalog_action_rejects_a_missing_required_cli_parameter(capsys, action_id):
    module = _metactl_module()
    action = _evolver_catalog().action(action_id)
    assert action is not None
    required = [name for name, spec in action["parameters"].items() if spec.get("required")]
    missing = required[0] if required else None
    arguments = ["--json", action_id]
    for name, spec in action["parameters"].items():
        if spec.get("required") and name != missing:
            arguments.extend([f"--{name.replace('_', '-')}", _cli_value(spec)])
    if missing is None:
        arguments.append("--not-a-catalog-parameter")

    with pytest.raises(SystemExit) as raised:
        module.main(arguments, transport=object())
    assert raised.value.code == 2
    assert "required" in capsys.readouterr().err or missing is None


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


def test_runs_group_supports_bounded_filters_and_json(capsys):
    module = _metactl_module()
    class Fake:
        def __init__(self): self.calls = []
        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"runs": []}
    fake = Fake()
    assert module.main(["runs", "list", "--controller-id", "edge/a", "--state", "paused", "--limit", "7", "--json"], transport=fake) == 0
    json.loads(capsys.readouterr().out)
    assert fake.calls == [("evolver.runs.list", {"controller_id": "edge/a", "state": "paused", "limit": 7})]


@pytest.mark.parametrize(("command", "action_id"), [
    (("control", "status"), "evolver.edge.status"),
    (("control", "controllers"), "evolver.controllers.list"),
    (("control", "instruments"), "evolver.instruments.list"),
    (("control", "runs"), "evolver.runs.list"),
    (("validation", "experiment"), "evolver.experiments.validate"),
])
def test_operator_group_aliases_map_to_existing_catalog_actions(capsys, command, action_id):
    module = _metactl_module()

    class Fake:
        def __init__(self): self.calls = []
        def action(self, action, parameters):
            self.calls.append((action, parameters))
            return {"action": action}

    fake = Fake()
    arguments = ["--json", *command]
    if action_id == "evolver.experiments.validate":
        arguments += ["--definition", "{}", "--resolved-at", "2026-01-01T00:00:00Z"]
    assert module.main(arguments, transport=fake) == 0
    json.loads(capsys.readouterr().out)
    expected = {"definition": {}, "selected_calibration_artifacts": [],
                "resolved_at": "2026-01-01T00:00:00Z"} if action_id.endswith("validate") else {}
    assert fake.calls == [(action_id, expected)]


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


def test_explicit_enrollment_action_redacts_all_enrollment_secrets(capsys):
    module = _metactl_module()

    class Fake:
        def action(self, action_id, parameters):
            return {"enrollment_token": "one-time", "credential": "durable-secret"}

    assert module.main(["--json", "controllers", "add", "https://edge", "--yes"], transport=Fake()) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"]["enrollment_token"] == "<redacted>"
    assert result["result"]["credential"] == "<redacted>"


def test_enrollment_accepts_endpoint_id_and_structured_release_binding(capsys):
    module = _metactl_module()

    class Fake:
        def __init__(self): self.calls = []
        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"ok": True}

    fake = Fake()
    binding = {"release": "r1", "source_revision": "abcdef1", "manifest_sha256": "a" * 64}
    assert module.main(["--json", "evolver.controllers.add", "--endpoint-id", "central",
                        "--release-binding", json.dumps(binding), "--yes"], transport=fake) == 0
    json.loads(capsys.readouterr().out)
    assert fake.calls == [("evolver.controllers.add", {
        "endpoint_id": "central", "release_binding": binding, "ttl_seconds": 900, "purpose": "enrollment",
    })]


def test_direct_command_result_unwraps_nested_disposition_and_physical_evidence(capsys):
    module = _metactl_module()

    class Fake:
        def action(self, action_id, parameters):
            return {"command": {"disposition": "accepted"}}

    assert module.main(["--json", "controllers", "refresh", "central-a"], transport=Fake()) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["disposition"] == "accepted"
    assert result["accepted_or_queued"] is True
    assert result["physical_actuation_verified"] is None


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
    assert result["result"]["physical_actuation_verified"] is None
    assert result["result"]["accepted"] is False
    assert result["result"]["queued"] is False
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    ("disposition", "physical", "accepted", "queued"),
    [
        ("accepted", None, True, False),
        ("queued", False, False, True),
        ("completed", True, False, False),
    ],
)
def test_commands_watch_report_preserves_disposition_and_tri_state_evidence(
    capsys, disposition, physical, accepted, queued
):
    module = _metactl_module()

    class Fake:
        def action(self, action_id, parameters):
            return {"command": {"disposition": disposition, **(
                {} if physical is None else {"physical_actuation_verified": physical}
            )}}

    assert module.main([
        "--json", "controllers", "commands", "watch", "central-a", "cmd-1",
        "--timeout", "0",
    ], transport=Fake()) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["disposition"] == disposition
    assert result["physical_actuation_verified"] is physical
    assert result["accepted"] is accepted
    assert result["queued"] is queued


def test_central_http_transport_binds_route_headers_and_body():
    module = _metactl_module()
    from tools.metactl_transport import HTTPTransport
    seen = {}
    def sender(url, method, body, headers, timeout):
        if url.endswith("/api/actions"):
            from operator_contract import build_manifest, load_snapshot
            return 200, json.dumps(build_manifest(load_snapshot())).encode()
        seen.update(url=url, method=method, body=body, headers=dict(headers), timeout=timeout)
        return 200, b'{"accepted":true}'
    client = HTTPTransport(base_url="http://central.test/", sender=sender,
                           operator="alice", shared_secret="proxy",
                           permissions="evolver:runs:write")
    # The run.* entries are planned catalog surfaces; the implemented
    # revision-fenced operator action is runs.pause.
    assert client.action("evolver.runs.pause", {"run_id": "run-a", "expected_revision": 1}) == {"accepted": True}
    assert seen["url"] == "http://central.test/api/evolver/runs/run-a/commands"
    assert seen["method"] == "POST"
    assert seen["body"] == {"action": "pause", "run_id": "run-a", "expected_revision": 1}
    assert seen["headers"]["X-Meta-Webui-Evolver-Operator"] == "alice"
    assert seen["headers"]["X-Meta-Webui-Evolver-Control-Secret"] == "proxy"

    seen.clear()
    bearer_client = HTTPTransport(base_url="http://gateway.test/", sender=sender,
                                  operator="alice", token="secret", shared_secret="proxy",
                                  permissions="evolver:runs:write")
    bearer_client.action("evolver.runs.pause", {"run_id": "run-a", "expected_revision": 1})
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert "X-Meta-Webui-Evolver-Operator" not in seen["headers"]
    assert "X-Meta-Webui-Evolver-Control-Secret" not in seen["headers"]


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


def test_interactive_mode_selects_from_catalog_and_dispatches():
    module = _metactl_module()

    class Fake:
        def __init__(self):
            self.calls = []

        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {"ok": True}

    fake = Fake()
    layout = module._layout(ROOT / "applications/deployment/action-catalog.json")
    available = [item for item in layout["actions"].values() if item["available"]]
    chosen = next(item for item in available if item["parameters"] == {})
    output = io.StringIO()
    assert module.main(["interactive"], transport=fake,
                       input=io.StringIO(f"{available.index(chosen) + 1}\n"),
                       output=output) == 0
    assert fake.calls == [(chosen["id"], {})]
    assert "Choose an action" in output.getvalue()


def test_central_http_transport_binds_release_build_route():
    from tools.metactl_transport import HTTPTransport
    seen = {}

    def sender(url, method, body, headers, timeout):
        if url.endswith("/api/actions"):
            from operator_contract import build_manifest, load_snapshot
            return 200, json.dumps(build_manifest(load_snapshot())).encode()
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


def test_legacy_entrypoints_are_forwarding_shims_only():
    for path in (ROOT / "metactl.py", ROOT / "tools/metactl.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert not [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        assert "_canonical" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["measurements", "telemetry", "activities", "events", "evidence", "logs"])
def test_fact_aliases_forward_controller_filters(capsys, kind):
    module = _metactl_module()

    class Fake:
        def __init__(self): self.calls = []
        def action(self, action_id, parameters):
            self.calls.append((action_id, parameters))
            return {kind: []}

    fake = Fake()
    assert module.main(["--json", "controllers", kind, "central-a", "--run-id", "run-1", "--limit", "4"], transport=fake) == 0
    json.loads(capsys.readouterr().out)
    assert fake.calls == [(f"evolver.controllers.{kind}", {"controller_id": "central-a", "run_id": "run-1", "limit": 4})]
