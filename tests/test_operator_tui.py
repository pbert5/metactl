import asyncio
import json
from dataclasses import dataclass

import pytest

pytest.importorskip("textual")

from metactl_transport import OperatorTarget
from operator_tui.app import (
    OperatorTUI,
    catalog_confirmation_label,
    catalog_drift,
    discovery_gate,
    load_navigation,
    physical_evidence_label,
    redact,
    safe_target_url,
    validate_action_parameters,
)


@dataclass
class FakeTransport:
    responses: dict[str, object]
    calls: list[tuple[str, dict]]

    def discover_actions(self):
        return {"actions": [{"id": name} for name in self.responses]}

    def action(self, action_id, parameters):
        self.calls.append((action_id, dict(parameters)))
        return self.responses[action_id]


def target():
    return OperatorTarget("http://central.test", "META_WEBUI_METACTL_CENTRAL_URL",
                          {"operator": True, "token": False, "shared_secret": False})


def test_startup_reports_target_and_unavailable_planned_actions():
    transport = FakeTransport({
        "evolver.edge.status": {"status": "ok"},
        "evolver.experiments.enqueue": {"status": "accepted"},
    }, [])
    app = OperatorTUI(transport=transport, target=target())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "http://central.test" in str(app.query_one("#connection").render())
            assert "reachable" in str(app.query_one("#connection").render())
            assert any(str(node.label) == "Controllers" for node in app.query_one("#navigation").root.children)
            assert "planned / unavailable" in str(app.query_one("#detail").render())
            assert "CLI: metactl" in str(app.query_one("#detail").render())
            assert "API: metactl api tui" in str(app.query_one("#detail").render())

    asyncio.run(scenario())


def test_tui_route_uses_operator_app_without_changing_api_workbench_route(monkeypatch):
    import cli
    import operator_tui.cli as operator_cli

    seen = {}
    def fake_main(argv, **kwargs):
        seen["argv"] = argv
        return 17
    monkeypatch.setattr(operator_cli, "main", fake_main)
    assert cli.main(["tui", "--help"]) == 17
    assert seen["argv"] == ["--help"]


def test_selecting_projection_dispatches_shared_read_and_preserves_raw_id():
    transport = FakeTransport({"evolver.controllers.show": {"controller_id": "edge/a", "state": "ready"}}, [])
    app = OperatorTUI(transport=transport, target=target())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            tree = app.query_one("#navigation")
            node = next(node for group in tree.root.children if str(group.label) == "Controllers"
                        for node in group.children if node.data == "evolver.controllers.show")
            tree.select_node(node)
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#parameter-controller_id").value = "edge/a"
            app.query_one("#run").focus()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            assert transport.calls == [("evolver.controllers.show", {"controller_id": "edge/a"})]
            assert '"controller_id": "edge/a"' in str(app.query_one("#detail").render())

    asyncio.run(scenario())


def test_mutation_requires_catalog_confirmation_and_distinguishes_accepted_from_physical():
    transport = FakeTransport({"evolver.controllers.archive": {
        "disposition": "accepted", "physical_actuation_verified": False,
    }}, [])
    app = OperatorTUI(transport=transport, target=target())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            tree = app.query_one("#navigation")
            node = next(node for group in tree.root.children if str(group.label) == "Controllers"
                        for node in group.children if node.data == "evolver.controllers.archive")
            tree.select_node(node)
            await pilot.press("enter")
            await pilot.pause()
            app.query_one("#parameter-controller_id").value = "edge/a"
            app.query_one("#run").focus()
            await pilot.press("enter")
            await pilot.pause()
            assert not transport.calls
            assert app.screen.__class__.__name__ == "ConfirmationScreen"
            app.query_one("#confirmation").value = "evolver.controllers.archive"
            app.query_one("#confirm-action").press()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert transport.calls == [("evolver.controllers.archive", {"controller_id": "edge/a"})]
            detail = str(app.query_one("#detail").render())
            assert "accepted/queued" in detail
            assert "physical evidence: no (explicit negative)" in detail

    asyncio.run(scenario())


def test_nested_command_result_is_presented_as_accepted_and_tri_state_physical_evidence():
    transport = FakeTransport({"evolver.controllers.archive": {
        "command": {"disposition": "queued"},
    }}, [])
    app = OperatorTUI(transport=transport, target=target())

    async def scenario():
        async with app.run_test(size=(120, 40)):
            app.show_result(transport.responses["evolver.controllers.archive"])
            detail = str(app.query_one("#detail").render())
            assert "accepted/queued: yes" in detail
            assert "physical evidence: unknown (not reported)" in detail
            assert '"disposition": "queued"' in detail

    asyncio.run(scenario())


def test_navigation_uses_shared_presentation_sections_and_keeps_recovery_top_level():
    app = OperatorTUI(transport=FakeTransport({}, []), target=target())
    labels = [str(node.label) for node in app.navigation_model]
    assert labels == ["Overview", "Controllers", "Instruments", "Runs", "Experiments",
                      "Releases", "Recovery", "Developer/API Workbench"]
    recovery = next(node for node in app.navigation_model if node.label == "Recovery")
    assert recovery.action_ids == (
        "evolver.controllers.recovery.request",
        "evolver.controllers.recovery.status",
        "evolver.controllers.recovery.diff",
    )


def test_navigation_does_not_recreate_actions_missing_from_presentation_model(tmp_path):
    presentation = tmp_path / "metactl-cli.json"
    presentation.write_text(json.dumps({
        "groups": {
            "controllers": {"show": "evolver.controllers.show"},
            "instruments": {"show": "evolver.instruments.show"},
        },
    }), encoding="utf-8")
    actions = {
        "evolver.controllers.show": {"id": "evolver.controllers.show"},
        "evolver.runs.show": {"id": "evolver.runs.show"},
        "evolver.instruments.show": {"id": "evolver.instruments.show"},
    }

    navigation = load_navigation(presentation, actions)

    runs = next(item for item in navigation if item.label == "Runs")
    assert runs.action_ids == ()
    controllers = next(item for item in navigation if item.label == "Controllers")
    assert controllers.action_ids == ("evolver.controllers.show",)


def test_target_and_response_redaction_preserve_shape_without_secrets():
    assert safe_target_url("https://user:pass@example.test/api?token=abc&keep=yes") == \
        "https://example.test/api?token=%3Credacted%3E&keep=yes"
    value = redact({"access_token": "abc", "nested": [{"password": "pw"}],
                    "url": "https://user:pass@example.test/?secret=abc", "ok": 3})
    assert value == {"access_token": "<redacted>", "nested": [{"password": "<redacted>"}],
                     "url": "https://example.test/?secret=%3Credacted%3E", "ok": 3}


def test_redaction_covers_enrollment_secret_fields_and_schemeless_urls():
    assert safe_target_url("//operator:password@example.test/api?token=abc&keep=yes") == \
        "//example.test/api?token=%3Credacted%3E&keep=yes"
    value = redact({"enrollment": {"password": "pw", "token": "tok",
                                    "shared_secret": "shared", "api_key": "key",
                                    "enrollment_token": "enroll"},
                    "url": "//operator:password@example.test/?api_key=abc"})
    assert value == {"enrollment": {"password": "<redacted>", "token": "<redacted>",
                                     "shared_secret": "<redacted>", "api_key": "<redacted>",
                                     "enrollment_token": "<redacted>"},
                     "url": "//example.test/?api_key=%3Credacted%3E"}


@pytest.mark.parametrize("url", [
    "HTTPS://operator:password@example.test/?TOKEN=secret",
    "http:///path?token=secret",
    "http://[bad/path?token=secret",
])
def test_safe_target_url_redacts_uppercase_empty_authority_and_malformed_urls(url):
    rendered = safe_target_url(url)
    assert "password" not in rendered
    assert "secret" not in rendered
    assert rendered in {"https://example.test/?TOKEN=%3Credacted%3E", "<redacted URL>"}


def test_physical_evidence_is_tri_state_and_catalog_confirmation_is_explicit():
    assert physical_evidence_label({}) == "unknown (not reported)"
    assert physical_evidence_label({"physical_actuation_verified": False}) == "no (explicit negative)"
    assert physical_evidence_label({"physical_actuation_verified": True}) == "yes"
    assert catalog_confirmation_label({"confirmation": "none", "effect": "read"}) == "SAFE / read-only"
    assert catalog_confirmation_label({"confirmation": "required", "effect": "mutation"}) == \
        "catalog confirmation: operator"
    assert catalog_confirmation_label({"confirmation": "physical", "effect": "hardware"}) == \
        "catalog confirmation: physical hardware"
    assert catalog_confirmation_label({"confirmation": "none"}, tags=("mutating",)) != "SAFE / read-only"
    assert catalog_confirmation_label({"confirmation": "none"}, tags=("mutating",)) == "catalog confirmation: none"


def test_tui_detail_uses_the_shared_human_cli_presentation_path():
    app = OperatorTUI(transport=FakeTransport({"evolver.controllers.show": {}}, []), target=target())
    assert app.cli_paths["evolver.controllers.show"] == "metactl controllers show"


def test_safe_stop_is_discoverable_in_controllers_without_a_lease_token_parameter():
    app = OperatorTUI(transport=FakeTransport({"evolver.controllers.safe_stop": {}}, []), target=target())
    controllers = next(item for item in app.navigation_model if item.label == "Controllers")
    assert "evolver.controllers.safe_stop" in controllers.action_ids
    action = app.actions["evolver.controllers.safe_stop"]
    assert tuple(action["parameters"]) == ("controller_id", "idempotency_key")
    assert app.cli_paths["evolver.controllers.safe_stop"] == "metactl controllers safe-stop"
    assert action["safety"] == {"risk": "high", "confirmation": "physical", "reversible": True, "effect": "hardware"}
    assert "lease_token" not in action["parameters"]
    assert "physical hardware" in catalog_confirmation_label(action["safety"])


def test_discovery_gate_requires_live_manifest_and_blocks_catalog_drift():
    local = {"version": "1.0.0", "api": {"a": {}, "b": {}}}
    assert discovery_gate(local, {"version": "1.0.0", "actions": [{"id": "a"}, {"id": "b"}]}) == (True, "clean")
    assert discovery_gate(local, None) == (False, "unavailable")
    assert discovery_gate(local, {"version": "1.0.0", "actions": [{"id": "a"}]}) == (False, "changed")
    assert catalog_drift(local, {"version": "2.0.0", "actions": [{"id": "a"}, {"id": "b"}]}) == "changed"


def test_catalog_parameter_validation_matches_required_type_and_enum_fields():
    action = {"parameters": {
        "count": {"type": "integer", "required": True},
        "mode": {"type": "string", "enum": ["safe", "fast"]},
        "enabled": {"type": "boolean"},
        "payload": {"type": "json"},
    }}
    assert validate_action_parameters(action, {"count": "2", "mode": "safe", "enabled": "true", "payload": '{"x": 1}'}) == {
        "count": 2, "mode": "safe", "enabled": True, "payload": {"x": 1}}
    with pytest.raises(ValueError, match="required"):
        validate_action_parameters(action, {})
    with pytest.raises(ValueError, match="enum"):
        validate_action_parameters(action, {"count": "2", "mode": "unsafe"})
    with pytest.raises(ValueError, match="integer"):
        validate_action_parameters(action, {"count": "2.5"})


def test_tui_never_dispatches_until_discovery_is_clean_or_for_planned_actions():
    transport = FakeTransport({"evolver.experiments.enqueue": {"disposition": "accepted"}}, [])
    app = OperatorTUI(transport=transport, target=target())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            app.selected_action = app.actions["evolver.experiments.enqueue"]
            app.dispatch("evolver.experiments.enqueue", {"definition": "{}"}, False)
            assert not transport.calls
            app.discovery_status = "clean"
            app.selected_action = app.actions["evolver.experiments.enqueue"]
            app.on_button_pressed(Button.Pressed(app.query_one("#run")))
            assert not transport.calls

    from textual.widgets import Button
    asyncio.run(scenario())
