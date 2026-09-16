import asyncio
from dataclasses import dataclass

import pytest

pytest.importorskip("textual")

from metactl_transport import OperatorTarget
from operator_tui.app import (
    OperatorTUI,
    catalog_confirmation_label,
    physical_evidence_label,
    redact,
    safe_target_url,
)


@dataclass
class FakeTransport:
    responses: dict[str, object]
    calls: list[tuple[str, dict]]

    def discover_actions(self):
        return {"version": "test", "actions": [{"id": name} for name in self.responses]}

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


def test_target_and_response_redaction_preserve_shape_without_secrets():
    assert safe_target_url("https://user:pass@example.test/api?token=abc&keep=yes") == \
        "https://example.test/api?token=%3Credacted%3E&keep=yes"
    value = redact({"access_token": "abc", "nested": [{"password": "pw"}],
                    "url": "https://user:pass@example.test/?secret=abc", "ok": 3})
    assert value == {"access_token": "<redacted>", "nested": [{"password": "<redacted>"}],
                     "url": "https://example.test/?secret=%3Credacted%3E", "ok": 3}


def test_physical_evidence_is_tri_state_and_catalog_confirmation_is_explicit():
    assert physical_evidence_label({}) == "unknown (not reported)"
    assert physical_evidence_label({"physical_actuation_verified": False}) == "no (explicit negative)"
    assert physical_evidence_label({"physical_actuation_verified": True}) == "yes"
    assert catalog_confirmation_label({"confirmation": "none", "effect": "read"}) == "SAFE / read-only"
    assert catalog_confirmation_label({"confirmation": "required", "effect": "mutation"}) == \
        "catalog confirmation: operator"
    assert catalog_confirmation_label({"confirmation": "physical", "effect": "hardware"}) == \
        "catalog confirmation: physical hardware"
