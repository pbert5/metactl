"""Trusted central transport used by the deployment ``metactl`` shell.

The CLI is a client of the central control plane.  It deliberately has no
local persistence or edge-runtime dependency.  ``InProcessTransport`` is for
tests and controlled composition; ``HTTPTransport`` is the production
boundary and can be given a sender fixture without opening a socket.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from http import HTTPStatus
from typing import Any, Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


Json = Any
Sender = Callable[[str, str, Json, Mapping[str, str], float], Any]
Dispatcher = Callable[[str, str, Json, Mapping[str, str]], Any]


@dataclass(frozen=True)
class Route:
    method: str
    template: str
    permission: str | None = None

    def path(self, parameters: Mapping[str, Any]) -> str:
        try:
            return self.template.format(**parameters)
        except (KeyError, ValueError) as exc:
            raise TransportError("bad_request", f"missing route parameter: {exc}") from exc


# These are central API routes, not implementation imports or arbitrary URLs.
ROUTE_BINDINGS: dict[str, Route] = {
    "evolver.edge.status": Route("GET", "/api/evolver/controllers"),
    "evolver.edge.controllers": Route("GET", "/api/evolver/controllers"),
    "evolver.edge.instruments": Route("GET", "/api/evolver/instruments"),
    "evolver.edge.runs": Route("GET", "/api/evolver/runs"),
    "evolver.run.start": Route("POST", "/api/evolver/runs/{run_id}/commands", "evolver:runs:write"),
    "evolver.run.pause": Route("POST", "/api/evolver/runs/{run_id}/commands", "evolver:runs:write"),
    "evolver.run.stop": Route("POST", "/api/evolver/runs/{run_id}/commands", "evolver:runs:write"),
    "evolver.controllers.list": Route("GET", "/api/evolver/controllers"),
    "evolver.controllers.show": Route("GET", "/api/evolver/controllers/{controller_id}"),
    "evolver.controllers.freshness": Route("GET", "/api/evolver/controllers/{controller_id}/sync-freshness"),
    "evolver.controllers.refresh": Route("POST", "/api/evolver/controllers/{controller_id}/refresh", "manage_controller"),
    "evolver.controllers.rescan": Route("POST", "/api/evolver/controllers/{controller_id}/hardware-rescan", "manage_controller"),
    "evolver.controllers.archive": Route("POST", "/api/evolver/controllers/{controller_id}/archive", "manage_controller"),
    "evolver.controllers.restore": Route("POST", "/api/evolver/controllers/{controller_id}/restore", "manage_controller"),
    "evolver.controllers.add": Route("POST", "/api/evolver/enrollment-tokens", "manage_controller"),
    "evolver.controllers.release.set": Route("POST", "/api/evolver/controllers/{controller_id}/desired-release", "update_controller"),
    "evolver.controllers.commands.list": Route("GET", "/api/evolver/controllers/{controller_id}/commands"),
    "evolver.controllers.commands.show": Route("GET", "/api/evolver/controllers/{controller_id}/commands/{command_id}"),
    "evolver.controllers.recovery.request": Route("POST", "/api/evolver/controllers/{controller_id}/recovery", "recover_controller"),
    "evolver.controllers.recovery.status": Route("GET", "/api/evolver/controllers/{controller_id}/recovery"),
    "evolver.controllers.recovery.diff": Route("GET", "/api/evolver/controllers/{controller_id}/recovery/diff"),
    "evolver.instruments.list": Route("GET", "/api/evolver/instruments"),
    "evolver.instruments.show": Route("GET", "/api/evolver/instruments/{instrument_id}"),
    "evolver.runs.list": Route("GET", "/api/evolver/runs"),
    "evolver.runs.show": Route("GET", "/api/evolver/runs/{run_id}"),
    "evolver.runs.pause": Route("POST", "/api/evolver/runs/{run_id}/commands", "operate_run"),
    "evolver.runs.resume": Route("POST", "/api/evolver/runs/{run_id}/commands", "operate_run"),
    "evolver.runs.stop": Route("POST", "/api/evolver/runs/{run_id}/commands", "operate_run"),
    "evolver.experiments.validate": Route("POST", "/api/evolver/experiments/validate"),
    "evolver.experiments.describe": Route("POST", "/api/evolver/experiments/describe"),
    "evolver.experiments.plan": Route("POST", "/api/evolver/experiments/plan"),
    "evolver.release.build": Route("POST", "/api/evolver/releases/build", "update_controller"),
}


class Transport(Protocol):
    def action(self, action_id: str, parameters: Mapping[str, Any]) -> Json: ...


class TransportError(RuntimeError):
    """Stable, non-secret error vocabulary for CLI and fixture consumers."""

    def __init__(self, kind: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.kind, self.status, self.message = kind, status, message

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind, "error": self.message}
        if self.status is not None:
            result["status"] = self.status
        return result


def _normalize(status: int, payload: Json) -> Json:
    if status in {401, 403, 404, 409}:
        kinds = {401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict"}
        message = payload.get("error") if isinstance(payload, Mapping) else None
        raise TransportError(kinds[status], str(message or HTTPStatus(status).phrase), status=status)
    if status >= 500:
        raise TransportError("central_failure", "central control plane failure", status=status)
    if status < 200 or status >= 300:
        raise TransportError("request_failed", f"central request failed ({status})", status=status)
    if not isinstance(payload, (dict, list)):
        raise TransportError("malformed_response", "central response must be a JSON object or array", status=status)
    return payload


def _decode(raw: Any, status: int) -> Json:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, bytes):
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError("malformed_response", "central response was not valid JSON", status=status) from exc
        if not isinstance(value, (dict, list)):
            raise TransportError("malformed_response", "central response JSON must be an object or array", status=status)
        return value
    raise TransportError("malformed_response", "central response was not JSON", status=status)


def _headers(*, operator: str | None, token: str | None, shared_secret: str | None,
             permissions: str | None) -> dict[str, str]:
    result = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        result["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    if operator:
        result["X-Meta-Webui-Evolver-Operator"] = operator
    if permissions:
        result["X-Meta-Webui-Evolver-Permissions"] = permissions
    if shared_secret:
        result["X-Meta-Webui-Evolver-Control-Secret"] = shared_secret
    return result


class InProcessTransport:
    def __init__(self, dispatcher: Dispatcher, *, headers: Mapping[str, str] | None = None) -> None:
        self.dispatcher, self.headers = dispatcher, dict(headers or {})

    def action(self, action_id: str, parameters: Mapping[str, Any]) -> Json:
        route = ROUTE_BINDINGS.get(action_id)
        if route is None:
            raise TransportError("unknown_action", f"no central route for {action_id}")
        body = None if route.method == "GET" else {"action": action_id.rsplit(".", 1)[-1], **dict(parameters)}
        try:
            response = self.dispatcher(route.method, route.path(parameters), body, self.headers)
            status, payload = response
            return _normalize(int(status), payload)
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError("network_failure", "central control plane is unavailable") from exc


class HTTPTransport:
    def __init__(self, *, base_url: str, timeout: float = 10, sender: Sender | None = None,
                 operator: str | None = None, token: str | None = None,
                 shared_secret: str | None = None, permissions: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.sender = sender or self._send
        self.headers = _headers(operator=operator, token=token, shared_secret=shared_secret, permissions=permissions)

    def action(self, action_id: str, parameters: Mapping[str, Any]) -> Json:
        route = ROUTE_BINDINGS.get(action_id)
        if route is None:
            raise TransportError("unknown_action", f"no central route for {action_id}")
        body = None if route.method == "GET" else {"action": action_id.rsplit(".", 1)[-1], **dict(parameters)}
        try:
            status, raw_payload = self.sender(self.base_url + route.path(parameters), route.method, body, self.headers, self.timeout)
            status = int(status)
            try:
                payload = _decode(raw_payload, status) if raw_payload is not None else None
            except TransportError:
                # Preserve the useful HTTP class even when an error body is bad.
                if status in {401, 403, 404, 409} or status >= 500:
                    payload = None
                else:
                    raise
            return _normalize(status, payload)
        except TransportError:
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise TransportError("network_failure", "central control plane is unavailable") from exc

    @staticmethod
    def _send(url: str, method: str, body: Json, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            with urlopen(Request(url, data=data, headers=dict(headers), method=method), timeout=timeout) as response:  # nosec B310
                return int(response.status), response.read()
        except HTTPError as exc:
            return int(exc.code), exc.read()


def configured_transport() -> HTTPTransport:
    return HTTPTransport(
        base_url=os.environ.get("META_WEBUI_METACTL_CENTRAL_URL", os.environ.get("META_WEBUI_EVOLVER_CONTROL_URL", "http://127.0.0.1:18087")),
        timeout=float(os.environ.get("META_WEBUI_METACTL_TIMEOUT", "10")),
        operator=os.environ.get("META_WEBUI_METACTL_OPERATOR"),
        token=os.environ.get("META_WEBUI_METACTL_TOKEN"),
        shared_secret=os.environ.get("META_WEBUI_EVOLVER_CONTROL_SHARED_SECRET"),
        permissions=os.environ.get("META_WEBUI_METACTL_PERMISSIONS"),
    )
