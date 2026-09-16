"""Small operator TUI; API Workbench remains a separate application."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, Static, Tree

try:
    from ..framework.action_catalog import load_action_catalog
    from ..metactl_transport import OperatorTarget, TransportError, resolve_operator_target
except ImportError:
    from framework.action_catalog import load_action_catalog
    from metactl_transport import OperatorTarget, TransportError, resolve_operator_target


CATALOG = Path(__file__).resolve().parents[1] / "applications" / "evolver" / "actions.json"


class ConfirmationScreen(ModalScreen[str | None]):
    def __init__(self, action_id: str) -> None:
        super().__init__()
        self.action_id = action_id

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(f"Mutation: {self.action_id}\nType the action ID to confirm.", markup=False),
            Input(id="confirmation"),
            Horizontal(Button("Confirm", id="confirm-action", variant="warning"),
                       Button("Cancel", id="cancel-action")),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-action":
            self.dismiss(None)
        elif event.button.id == "confirm-action":
            value = self.query_one("#confirmation", Input).value
            if value == self.action_id:
                self.dismiss(value)
            else:
                self.notify("Confirmation does not match", severity="error")


class OperatorTUI(App[None]):
    TITLE = "metactl operator"
    CSS = """
    #connection { height: 3; padding: 1; border: round $primary; }
    #navigation { width: 42%; border: round $primary; }
    #detail-pane { width: 58%; border: round $primary; padding: 1; }
    #detail { height: 1fr; }
    #parameters { height: auto; max-height: 12; }
    #run { width: 1fr; }
    """

    def __init__(self, *, transport: Any, target: OperatorTarget | None = None) -> None:
        super().__init__()
        self.transport = transport
        self.target = target or resolve_operator_target()
        self.catalog = load_action_catalog(CATALOG)
        self.actions = {action["id"]: action for action in self.catalog.actions}
        self.selected_action: Mapping[str, Any] | None = None
        self.connection = "checking"

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self.connection_text(), id="connection", markup=False)
        with Horizontal():
            yield Tree("operator actions", id="navigation")
            with Vertical(id="detail-pane"):
                yield Static("Select an action. Planned actions are planned / unavailable.\nCLI: metactl <noun> <action>\nAPI: metactl api tui", id="detail", markup=False)
                yield Vertical(id="parameters")
                yield Button("Run", id="run", variant="primary")
        yield Footer()

    def connection_text(self) -> str:
        auth = ", ".join(name for name, present in self.target.auth.items() if present) or "none"
        return f"target: {self.target.url} ({self.target.source}) | connection: {self.connection} | auth configured: {auth}"

    def on_mount(self) -> None:
        self.populate_tree()
        self.run_worker(self.probe_connection(), name="connection", exclusive=True)

    async def probe_connection(self) -> None:
        try:
            self.transport.discover_actions()
        except Exception:
            self.connection = "unavailable"
        else:
            self.connection = "reachable"
        self.query_one("#connection", Static).update(self.connection_text())

    def populate_tree(self) -> None:
        tree = self.query_one("#navigation", Tree)
        groups: dict[str, Any] = {}
        for action in self.actions.values():
            noun = action["id"].split(".")[1] if "." in action["id"] else action["id"]
            group = groups.setdefault(noun, tree.root.add(noun, expand=True))
            planned = action["status"] != "implemented"
            badge = "planned / unavailable" if planned else action["status"]
            group.add_leaf(f"{action['title']} [{badge}]", data=action["id"])
        tree.root.expand()

    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        action_id = event.node.data
        if not isinstance(action_id, str):
            return
        self.selected_action = self.actions[action_id]
        action = self.selected_action
        planned = action["status"] != "implemented"
        self.query_one("#detail", Static).update(
            f"{action['title']}\nAction ID: {action_id}\nStatus: {action['status']}"
            + ("\nplanned / unavailable" if planned else ""))
        parameters = self.query_one("#parameters")
        await parameters.remove_children()
        for name, spec in action.get("parameters", {}).items():
            await parameters.mount(Label(name, markup=False), Input(id=f"parameter-{name}"))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "run" or not self.selected_action:
            return
        if self.selected_action["status"] != "implemented":
            self.notify("This planned action is unavailable", severity="warning")
            return
        action_id = self.selected_action["id"]
        parameters = {
            name: self.query_one(f"#parameter-{name}", Input).value
            for name in self.selected_action.get("parameters", {})
            if self.query_one(f"#parameter-{name}", Input).value != ""
        }
        safety = self.selected_action.get("safety", {})
        if safety.get("confirmation") not in (None, "none") or safety.get("effect") not in (None, "read"):
            self.push_screen(ConfirmationScreen(action_id), lambda answer: self.dispatch(action_id, parameters, bool(answer)))
        else:
            self.dispatch(action_id, parameters, False)

    def dispatch(self, action_id: str, parameters: dict[str, Any], confirmed: bool) -> None:
        if self.selected_action and self.selected_action.get("safety", {}).get("confirmation") not in (None, "none") and not confirmed:
            return
        try:
            result = self.transport.action(action_id, parameters)
        except TransportError as error:
            result = error.as_dict()
        self.show_result(result)

    def show_result(self, result: Any) -> None:
        if isinstance(result, Mapping) and result.get("disposition") in {"accepted", "queued"}:
            disposition = "accepted/queued"
            physical = "yes" if result.get("physical_actuation_verified") is True else "no"
            text = f"{disposition}\nphysical evidence: {physical}\n{json.dumps(result, indent=2, sort_keys=True)}"
        else:
            text = json.dumps(result, indent=2, sort_keys=True, default=str)
        self.query_one("#detail", Static).update(text)
