"""Small operator TUI; API Workbench remains a separate application."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


APPLICATIONS = Path(__file__).resolve().parents[1] / "applications"
CATALOG = APPLICATIONS / "evolver" / "actions.json"
PRESENTATION = APPLICATIONS / "deployment" / "metactl-cli.json"
SENSITIVE_PARTS = ("credential", "password", "secret", "token", "private_key", "api_key", "authorization")


@dataclass(frozen=True)
class NavigationItem:
    label: str
    action_ids: tuple[str, ...] = ()
    workbench: bool = False


WORKBENCH_NODE = object()


def safe_target_url(value: str) -> str:
    """Remove URL userinfo and redact sensitive query parameters for display."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return "<redacted URL>"
    if not parts.scheme or not parts.netloc:
        return value
    try:
        hostname = parts.hostname or ""
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        return "<redacted URL>"
    query = urlencode([
        (key, "<redacted>" if any(part in key.lower() for part in SENSITIVE_PARTS) else item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ])
    return urlunsplit((parts.scheme, hostname + port, parts.path, query, ""))


def redact(value: Any, *, key: str | None = None) -> Any:
    """Redact secret-shaped response fields and embedded URLs recursively."""
    if key is not None and any(part in key.lower() for part in SENSITIVE_PARTS):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {name: redact(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str) and (value.startswith(("http://", "https://"))):
        return safe_target_url(value)
    return value


def physical_evidence_label(result: Mapping[str, Any]) -> str:
    value = result.get("physical_actuation_verified")
    if value is True:
        return "yes"
    if value is False:
        return "no (explicit negative)"
    return "unknown (not reported)"


def catalog_confirmation_label(safety: Mapping[str, Any]) -> str:
    confirmation = safety.get("confirmation", "none")
    effect = safety.get("effect", "read")
    if confirmation == "none" and effect == "read":
        return "SAFE / read-only"
    if confirmation == "physical" or effect == "hardware":
        return "catalog confirmation: physical hardware"
    if confirmation == "operator" or confirmation == "required":
        return "catalog confirmation: operator"
    return f"catalog confirmation: {confirmation}"


def _action_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Mapping):
        return []
    if isinstance(value.get("action_id"), str):
        return [value["action_id"]]
    result: list[str] = []
    for name, child in value.items():
        if name in {"positionals", "defaults"}:
            continue
        result.extend(_action_ids(child))
    return result


def load_navigation(presentation: Path, actions: Mapping[str, Mapping[str, Any]]) -> tuple[NavigationItem, ...]:
    document = json.loads(presentation.read_text(encoding="utf-8"))
    groups = document.get("groups", {})

    def group_ids(name: str) -> list[str]:
        return [identifier for identifier in _action_ids(groups.get(name, {})) if identifier in actions]

    controller_group = groups.get("controllers", {})
    recovery_ids = [identifier for identifier in _action_ids(
        controller_group.get("recovery", {}) if isinstance(controller_group, Mapping) else {}
    ) if identifier in actions]
    controller_ids = [identifier for identifier in group_ids("controllers") if identifier not in recovery_ids]
    return (
        NavigationItem("Overview", tuple(group_ids("overview"))),
        NavigationItem("Controllers", tuple(controller_ids)),
        NavigationItem("Instruments", tuple(group_ids("instruments"))),
        NavigationItem("Runs", tuple(group_ids("runs"))),
        NavigationItem("Experiments", tuple(group_ids("experiments"))),
        NavigationItem("Releases", tuple(group_ids("releases"))),
        NavigationItem("Recovery", tuple(recovery_ids)),
        NavigationItem("Developer/API Workbench", workbench=True),
    )


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
        self.navigation_model = load_navigation(PRESENTATION, self.actions)
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
        return f"target: {safe_target_url(self.target.url)} ({self.target.source}) | connection: {self.connection} | auth configured: {auth} | mode: SAFE"

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
        for item in self.navigation_model:
            group = tree.root.add(item.label, expand=True, data=item.label)
            if item.workbench:
                group.add_leaf("Open API Workbench", data=WORKBENCH_NODE)
            for action_id in item.action_ids:
                action = self.actions[action_id]
                planned = action["status"] != "implemented"
                badge = "planned / unavailable" if planned else action["status"]
                group.add_leaf(f"{action['title']} [{badge}]", data=action_id)
        tree.root.expand()

    async def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        action_id = event.node.data
        if not isinstance(action_id, str):
            if action_id is WORKBENCH_NODE:
                self.selected_action = None
                self.query_one("#detail", Static).update("Developer/API Workbench\nUse: metactl api tui\nAPI Workbench is a separate application.")
            return
        self.selected_action = self.actions[action_id]
        action = self.selected_action
        planned = action["status"] != "implemented"
        self.query_one("#detail", Static).update(
            f"{action['title']}\nAction ID: {action_id}\nStatus: {action['status']}\n"
            f"{catalog_confirmation_label(action.get('safety', {}))}"
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
        if safety.get("confirmation") not in (None, "none"):
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
        result = redact(result)
        if isinstance(result, Mapping) and result.get("disposition") in {"accepted", "queued"}:
            disposition = "accepted/queued"
            physical = physical_evidence_label(result)
            text = f"{disposition}\nphysical evidence: {physical}\n{json.dumps(result, indent=2, sort_keys=True)}"
        else:
            text = json.dumps(result, indent=2, sort_keys=True, default=str)
        self.query_one("#detail", Static).update(text)
