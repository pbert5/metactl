"""Specialized Textual developer workbench, separate from configured pages."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static, TabbedContent, TabPane, TextArea, Tree, RichLog

from .model import WorkbenchError
from .execution import HTTPClient, FixtureClient, Session, prepare, response_diff, export_request
from .evidence import resolve_tests, run_tests


class Confirmation(ModalScreen[str | None]):
    DEFAULT_CSS = """
    Confirmation { align: center middle; }
    Confirmation > Vertical { width: 72; height: auto; padding: 1 2; border: thick $warning; background: $surface; }
    Confirmation Label { height: auto; margin-bottom: 1; }
    """

    def __init__(self, expected, message):
        super().__init__()
        self.expected, self.message = expected, message

    def compose(self):
        with Vertical():
            yield Label(self.message, markup=False)
            yield Label(f"Type {self.expected} to continue", markup=False)
            yield Input(id="confirmation")
            with Horizontal():
                yield Button("Confirm", id="confirm", variant="warning")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event):
        if event.button.id == "cancel":
            self.dismiss(None)
        elif self.query_one(Input).value == self.expected:
            self.dismiss(self.expected)
        else:
            self.notify("Confirmation does not match", severity="error")


class WorkbenchApp(App):
    TITLE = "API Workbench"
    CSS = """
    #session { height: 3; }
    #mode { width: 28; content-align: left middle; }
    #target { width: 1fr; }
    #workspace { height: 3fr; min-height: 12; }
    #navigation { width: 26%; min-width: 22; border: round $primary; }
    #request { width: 44%; border: round $primary; padding: 0 1; }
    #contract-pane { width: 30%; border: round $primary; }
    #contract { height: auto; }
    #endpoint-tree { height: 1fr; }
    #parameters { height: 1fr; min-height: 3; }
    #body { height: 6; }
    #request-headers { height: 3; }
    #controls { height: 3; }
    #controls Button { min-width: 8; width: 1fr; }
    #expectations { height: 3; }
    #response-tabs { height: 2fr; min-height: 9; }
    #response-status { height: 1; }
    #history { height: 6; }
    TextArea { border: none; }
    #operation { height: auto; max-height: 3; }
    #evidence-controls { height: 3; }
    #evidence-controls Button { min-width: 10; }
    """
    BINDINGS = [Binding("ctrl+q", "quit", "Quit"), Binding("/", "search", "Search"),
                Binding("ctrl+enter", "send", "Send"), Binding("t", "send", "Check response"),
                Binding("T", "suite", "Evidence suite"), Binding("f", "evidence", "Evidence"),
                Binding("c", "curl", "curl"), Binding("p", "python", "Python"),
                Binding("h", "history", "History"), Binding("e", "edit", "Edit JSON"),
                Binding("escape", "cancel", "Cancel tests")]

    def __init__(self, registry, session=None, drift=None, audit=None):
        super().__init__()
        self.registry = registry
        self.session = session or Session()
        self.drift = drift  # None means not compared, never zero drift.
        self.audit = audit
        self.endpoint = None
        self.field_names = {}
        self.current_exchange = None
        self.inflight = False
        self.test_cancel = threading.Event()
        self.tests_running = False
        self.watch_enabled = False
        self.last_request = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="session"):
            yield Static(self.mode_label(), id="mode", markup=False)
            yield Input(self.session.client.base_url if self.session.client else "", placeholder="Server URL (blank = offline)", id="target", disabled=isinstance(self.session.client, FixtureClient))
        with Horizontal(id="workspace"):
            with Vertical(id="navigation"):
                yield Input(placeholder="Search method, action, status...", id="search")
                yield Tree("Endpoints", id="endpoint-tree")
            with Vertical(id="request"):
                yield Static("Select an action", id="operation", markup=False)
                yield VerticalScroll(id="parameters")
                yield Label("JSON body (null = generate from parameters)")
                yield TextArea("null", id="body", soft_wrap=True)
                yield Label("Additional headers (JSON; auth comes from metactl environment)")
                yield TextArea("{}", id="request-headers", soft_wrap=True)
                with Horizontal(id="expectations"):
                    yield Input(placeholder="Expected status (default 2xx)", id="expected")
                    yield Input(placeholder="Max latency ms", id="latency")
                with Horizontal(id="controls"):
                    yield Button("Send", id="send", variant="primary")
                    yield Button("Body", id="generate")
                    yield Button("Watch", id="watch")
            with VerticalScroll(id="contract-pane"):
                yield Static("Contract", id="contract", markup=False)
        yield Static("No request sent", id="response-status", markup=False)
        with TabbedContent(id="response-tabs"):
            with TabPane("JSON", id="json-tab"):
                yield TextArea(read_only=True, id="json-response")
            with TabPane("Tree", id="tree-tab"):
                yield Tree("Response", id="response-tree")
            with TabPane("Raw", id="raw-tab"):
                yield TextArea(read_only=True, id="raw-response")
            with TabPane("Headers", id="headers-tab"):
                yield TextArea(read_only=True, id="response-headers")
            with TabPane("Contract", id="contract-tab"):
                yield TextArea(read_only=True, id="response-contract")
            with TabPane("Diff", id="diff-tab"):
                yield TextArea(read_only=True, id="diff-response")
            with TabPane("Checks", id="checks-tab"):
                yield TextArea(read_only=True, id="checks")
            with TabPane("Drift", id="drift-tab"):
                yield TextArea(json.dumps(self.drift, indent=2) if self.drift is not None else "Not compared. Start with --repo and --server.", read_only=True, id="drift")
            with TabPane("Audit", id="audit-tab"):
                yield TextArea(json.dumps(self.audit, indent=2) if self.audit else "Use --audit for a static route audit.", read_only=True)
            with TabPane("Evidence", id="evidence-tab"):
                with Horizontal(id="evidence-controls"):
                    yield Button("Endpoint tests", id="evidence")
                    yield Button("App suite", id="suite")
                    yield Button("Cancel", id="cancel-tests")
                yield RichLog(id="test-log", wrap=True, markup=False, highlight=False, max_lines=1000)
            with TabPane("Export", id="export-tab"):
                yield TextArea(read_only=True, id="export")
        yield DataTable(id="history", cursor_type="row")
        yield Footer()

    def mode_label(self):
        mode = "SIMULATOR / FIXTURE" if isinstance(self.session.client, FixtureClient) else "HARDWARE ENABLED" if self.session.policy.allow_hardware else "MUTATIONS ENABLED" if self.session.policy.allow_mutations else "SAFE"
        return mode + (f" | drift {len(self.drift)}" if self.drift is not None else "")

    def on_mount(self):
        self.query_one("#history", DataTable).add_columns("Time", "Action", "Status", "ms", "Checks")
        self.populate_tree("")
        self.set_interval(2, self.poll_watch)

    def populate_tree(self, query):
        tree = self.query_one("#endpoint-tree", Tree)
        tree.clear()
        groups = {}
        for endpoint in self.registry.select(query):
            group_key = (endpoint.application, endpoint.id.rsplit(".", 1)[0])
            if endpoint.application not in groups:
                groups[endpoint.application] = tree.root.add(endpoint.application, expand=True)
            if group_key not in groups:
                groups[group_key] = groups[endpoint.application].add(group_key[1], expand=True)
            groups[group_key].add_leaf(f"{endpoint.method or '-'} {endpoint.id.rsplit('.', 1)[-1]} [{endpoint.badge}] {endpoint.status}", data=endpoint.id)
        tree.root.expand()

    def on_input_changed(self, event):
        if event.input.id == "search":
            self.populate_tree(event.value)

    async def on_tree_node_selected(self, event):
        if event.control.id != "endpoint-tree" or not event.node.data:
            return
        self.watch_enabled = False
        self.endpoint = self.registry.endpoints[event.node.data]
        endpoint = self.endpoint
        self.query_one("#operation", Static).update(f"{endpoint.method or 'NO API'} {endpoint.path or endpoint.id} [{endpoint.badge}]")
        self.query_one("#contract", Static).update(json.dumps(endpoint.contract() | {"source": endpoint.source}, indent=2))
        pane = self.query_one("#parameters", VerticalScroll)
        await pane.remove_children()
        self.field_names = {}
        for index, (name, spec) in enumerate(endpoint.parameters.items()):
            field_id = f"param-{index}"
            self.field_names[field_id] = name
            default = spec.get("default", "")
            text = default if isinstance(default, str) else json.dumps(default)
            hint = f"{name} ({spec['in']}, {spec.get('type', 'any')})" + (" required" if spec.get("required") else "")
            if "enum" in spec:
                hint += " choices=" + json.dumps(spec["enum"])
            await pane.mount(Label(hint, markup=False), Input(value=text, id=field_id, password=bool(__import__('re').search('secret|password|token|credential', name, __import__('re').I))))
        self.query_one("#body", TextArea).load_text("null")

    def values(self):
        return {name: self.query_one("#" + field_id, Input).value for field_id, name in self.field_names.items() if self.query_one("#" + field_id, Input).value != ""}

    def on_button_pressed(self, event):
        actions = {"send": self.action_send, "watch": self.action_watch, "generate": self.action_generate,
                   "evidence": self.action_evidence, "suite": self.action_suite, "cancel-tests": self.action_cancel}
        callback = actions.get(event.button.id)
        if callback:
            callback()

    def action_generate(self):
        if self.endpoint:
            try:
                _, body, _ = prepare(self.endpoint, self.values())
                self.query_one("#body", TextArea).load_text(json.dumps(body, indent=2))
            except (ValueError, TypeError) as exc:
                self.notify(str(exc), severity="error")

    def action_send(self):
        if not self.endpoint or self.inflight:
            return
        try:
            target = self.query_one("#target", Input).value.strip()
            if not isinstance(self.session.client, FixtureClient):
                if not target:
                    raise WorkbenchError("enter a server URL or use fixtures")
                if self.session.client is None or self.session.client.base_url != target.rstrip("/"):
                    self.session.client = HTTPClient(target)
                    self.drift = None
                    self.query_one("#drift", TextArea).load_text("Target changed; previous drift comparison is no longer valid.")
                    self.notify("Target changed; authentication headers cleared")
            body = json.loads(self.query_one("#body", TextArea).text)
            headers = json.loads(self.query_one("#request-headers", TextArea).text)
            if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
                raise WorkbenchError("headers must be a JSON object with string values")
            expected = self.query_one("#expected", Input).value
            latency = self.query_one("#latency", Input).value
            options = dict(body=body, headers=headers, expected_status=int(expected) if expected else None, latency_ms=float(latency) if latency else None)
            if options["expected_status"] is not None and not 100 <= options["expected_status"] <= 599:
                raise WorkbenchError("expected status must be 100..599")
            if options["latency_ms"] is not None and options["latency_ms"] <= 0:
                raise WorkbenchError("latency threshold must be positive")
            endpoint, values = self.endpoint, self.values()
            prepare(endpoint, values, body, headers)
            # Check session permissions before opening a confirmation dialog.
            self.session.policy.check(endpoint, fixture=isinstance(self.session.client, FixtureClient), confirmation=endpoint.id)
            if endpoint.effect != "read":
                self.push_screen(Confirmation(endpoint.id, f"{endpoint.method} {endpoint.path}\nTarget: {target}\nEffect: {endpoint.effect}\nAn HTTP acknowledgement is not physical evidence."),
                                 lambda answer: self.start_request(endpoint, values, options | {"confirmation": answer}) if answer else None)
            else:
                self.start_request(endpoint, values, options)
        except (ValueError, TypeError) as exc:
            self.watch_enabled = False
            self.notify(str(exc), severity="error")

    def start_request(self, endpoint, values, options):
        if self.inflight:
            return
        self.inflight = True
        self.query_one("#send", Button).disabled = True
        self.last_request = (endpoint, values, options)
        self.send_request(endpoint, values, options)

    @work(thread=True, group="http", exit_on_error=False)
    def send_request(self, endpoint, values, options):
        try:
            result = self.session.execute(endpoint, values, **options)
            self.call_from_thread(self.finished, result, None)
        except Exception as exc:
            self.call_from_thread(self.finished, None, str(exc) if isinstance(exc, WorkbenchError) else type(exc).__name__)

    def finished(self, exchange, error):
        self.inflight = False
        self.query_one("#send", Button).disabled = False
        if error:
            self.watch_enabled = False
            self.notify(error, severity="error")
            return
        self.show_exchange(exchange)
        table = self.query_one("#history", DataTable)
        table.clear()
        for index, item in enumerate(self.session.history):
            import datetime
            table.add_row(datetime.datetime.fromtimestamp(item.timestamp).strftime("%H:%M:%S"), item.action_id,
                          str(item.status or "ERROR"), f"{item.elapsed_ms:.0f}", "FAIL" if item.error or any(c["passed"] is False for c in item.checks) else "OK", key=str(index))

    def show_exchange(self, exchange):
        previous = self.current_exchange
        self.current_exchange = exchange
        self.query_one("#response-status", Static).update(f"{exchange.status or 'ERROR'} | {exchange.elapsed_ms:.1f} ms | {exchange.size} bytes | {exchange.error or exchange.action_id}")
        for selector, text in {"#json-response": exchange.raw, "#raw-response": "Sanitized JSON; original wire bytes are not retained.\n" + exchange.raw,
                               "#response-headers": json.dumps(exchange.response_headers, indent=2), "#response-contract": json.dumps(exchange.contract, indent=2),
                               "#checks": json.dumps(exchange.checks, indent=2), "#diff-response": response_diff(previous, exchange) if previous else "Select another response to compare."}.items():
            self.query_one(selector, TextArea).load_text(text)
        tree = self.query_one("#response-tree", Tree)
        tree.clear()
        try:
            value = json.loads(exchange.raw)
        except ValueError:
            value = exchange.raw
        budget = [1000]
        def add(node, value, depth=0):
            if budget[0] <= 0 or depth >= 20:
                node.add_leaf("... view JSON for remaining content")
                return
            pairs = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else None
            if pairs is None:
                node.add_leaf(str(value))
                budget[0] -= 1
                return
            for key, item in pairs:
                if budget[0] <= 0:
                    break
                budget[0] -= 1
                child = node.add(str(key))
                add(child, item, depth + 1)
        add(tree.root, value)
        tree.root.expand()

    def on_data_table_row_selected(self, event):
        if event.control.id == "history":
            self.show_exchange(list(self.session.history)[int(event.row_key.value)])

    def action_watch(self):
        if not self.endpoint or self.endpoint.effect != "read":
            self.notify("Watch only supports read-only endpoints", severity="error")
            return
        self.watch_enabled = not self.watch_enabled
        self.query_one("#watch", Button).label = "Stop" if self.watch_enabled else "Watch"
        if self.watch_enabled:
            self.action_send()

    def poll_watch(self):
        if self.watch_enabled and not self.inflight:
            self.action_send()

    def action_evidence(self):
        self.start_tests(False)

    def action_suite(self):
        self.start_tests(True)

    def start_tests(self, suite):
        if not self.endpoint or self.tests_running:
            return
        try:
            endpoints = self.registry.select(application=self.endpoint.application) if suite else [self.endpoint]
            targets = []
            for endpoint in endpoints:
                if endpoint.evidence.get("tests"):
                    targets.extend(resolve_tests(endpoint))
            if not targets:
                raise WorkbenchError("no local pytest evidence available")
            self.push_screen(Confirmation("RUN TESTS", f"Run {len(set(targets))} local pytest targets?\nTests execute repository Python code and fixtures. This is not an HTTP safety sandbox."),
                             lambda answer: self.launch_tests(targets) if answer else None)
        except WorkbenchError as exc:
            self.notify(str(exc), severity="error")

    def launch_tests(self, targets):
        self.test_cancel.clear()
        self.tests_running = True
        self.query_one("#response-tabs", TabbedContent).active = "evidence-tab"
        self.test_worker(targets)

    @work(thread=True, group="tests", exit_on_error=False)
    def test_worker(self, targets):
        def emit(text):
            self.call_from_thread(self.query_one("#test-log", RichLog).write, text)
        try:
            results = run_tests(targets, trusted=True, emit=emit, cancel=self.test_cancel)
            emit(json.dumps(results, indent=2))
        except WorkbenchError as exc:
            emit(str(exc))
        finally:
            self.call_from_thread(self.tests_finished)

    def tests_finished(self):
        self.tests_running = False

    def action_cancel(self):
        self.test_cancel.set()
        self.watch_enabled = False

    def on_unmount(self):
        self.test_cancel.set()

    def action_search(self):
        self.query_one("#search", Input).focus()

    def action_edit(self):
        self.query_one("#body", TextArea).focus()

    def action_history(self):
        self.query_one("#history", DataTable).focus()

    def export(self, language):
        if self.current_exchange:
            text = export_request(self.current_exchange, language)
            self.query_one("#export", TextArea).load_text(text)
            self.query_one("#response-tabs", TabbedContent).active = "export-tab"
            self.copy_to_clipboard(text)
            self.notify("Sanitized export copied; replace redacted credentials before use")

    def action_curl(self):
        self.export("curl")

    def action_python(self):
        self.export("python")
