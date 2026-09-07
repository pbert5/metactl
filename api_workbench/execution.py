"""Bounded HTTP, validated requests, explicit safety and redacted history."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from collections import deque
import difflib
import json
import math
import re
import shlex
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .model import Endpoint, WorkbenchError

LIMIT = 2 * 1024 * 1024
SENSITIVE = re.compile(r"authorization|cookie|password|secret|token|credential|private.?key|api.?key", re.I)


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {k: "<redacted>" if SENSITIVE.search(str(k)) else redact(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
            value = value.replace(secret, "<redacted>")
            value = value.replace(quote(secret, safe=""), "<redacted>")
        return value
    return value


def secret_values(value):
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            if SENSITIVE.search(str(key)):
                result.extend(str(v) for v in flatten(item))
            else:
                result.extend(secret_values(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(secret_values(item))
    return result


def flatten(value):
    if isinstance(value, dict):
        return [v for x in value.values() for v in flatten(x)]
    if isinstance(value, list):
        return [v for x in value for v in flatten(x)]
    return [value]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPClient:
    def __init__(self, base_url, headers=None, timeout=10):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise WorkbenchError("server must be an HTTP(S) URL without credentials, query or fragment")
        if not 0 < timeout <= 300:
            raise WorkbenchError("timeout must be greater than zero and at most 300 seconds")
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def send(self, method, path, body=None, headers=None):
        if not path.startswith("/") or path.startswith("//"):
            raise WorkbenchError("request path must remain on the selected server")
        combined = {**self.headers, **(headers or {})}
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        if data is not None and len(data) > LIMIT:
            raise WorkbenchError("request exceeds 2 MiB limit")
        if data is not None:
            combined.setdefault("Content-Type", "application/json")
        combined.setdefault("Accept", "application/json")
        if any("\n" in str(k) + str(v) or "\r" in str(k) + str(v) for k, v in combined.items()):
            raise WorkbenchError("headers cannot contain line breaks")
        try:
            try:
                response = self.opener.open(Request(self.base_url + path, data=data, headers=combined, method=method), timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                raw = response.read(LIMIT + 1)
                if len(raw) > LIMIT:
                    raise WorkbenchError("response exceeds 2 MiB limit")
                return response.code, dict(response.headers), raw
        except (URLError, TimeoutError, OSError) as error:
            # URLs, proxy settings and credentials can occur in exception text.
            raise WorkbenchError(f"HTTP transport failed ({type(error).__name__})") from error

    def read(self, path):
        return self.send("GET", path)


class FixtureClient:
    """Deterministic offline execution. There is no HTTP fallback."""
    base_url = "fixture://offline"
    headers = {}

    def __init__(self, records):
        self.records = records

    def send(self, method, path, body=None, headers=None):
        for record in self.records:
            if record["method"] == method and record["path"] == path and record.get("request_body") == body:
                raw = record.get("raw")
                return int(record["status"]), record.get("headers", {}), (raw.encode() if raw is not None else json.dumps(record.get("body")).encode())
        raise WorkbenchError("no matching fixture for this exact method, path and body")


def validate_schema(value, schema):
    if schema is None:
        return
    try:
        from jsonschema import validators, ValidationError, SchemaError
    except ImportError as exc:
        raise WorkbenchError("install metactl[workbench] to validate JSON Schema") from exc
    # All refs must remain within the snapshot, including catalog schemas.
    def local(item):
        if isinstance(item, dict):
            if "$ref" in item and (not isinstance(item["$ref"], str) or not item["$ref"].startswith("#/")):
                raise WorkbenchError("external schema references are disabled")
            for v in item.values():
                local(v)
        elif isinstance(item, list):
            for v in item:
                local(v)
    local(schema)
    # Normalize OpenAPI 3.0 nullable for JSON Schema validation.
    def normalize(item):
        if isinstance(item, list):
            return [normalize(v) for v in item]
        if not isinstance(item, dict):
            return item
        item = {k: normalize(v) for k, v in item.items()}
        if item.pop("nullable", False) and isinstance(item.get("type"), str):
            item["type"] = [item["type"], "null"]
        if isinstance(item.get("exclusiveMinimum"), bool):
            exclusive = item.pop("exclusiveMinimum")
            if exclusive and "minimum" in item:
                item["exclusiveMinimum"] = item.pop("minimum")
        if isinstance(item.get("exclusiveMaximum"), bool):
            exclusive = item.pop("exclusiveMaximum")
            if exclusive and "maximum" in item:
                item["exclusiveMaximum"] = item.pop("maximum")
        return item
    schema = normalize(schema)
    try:
        cls = validators.validator_for(schema)
        cls.check_schema(schema)
        cls(schema).validate(value)
    except (ValidationError, SchemaError) as exc:
        # Do not print the invalid instance, which may contain credentials.
        raise WorkbenchError(f"schema validation failed at {list(exc.path)} ({exc.validator})") from exc


def coerce(raw, spec):
    kind = spec.get("type", "string")
    if isinstance(raw, str) and kind != "string" and kind != ["string", "null"]:
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise WorkbenchError(f"expected {kind} encoded as JSON") from exc
    valid = {"string": isinstance(raw, str), "integer": isinstance(raw, int) and not isinstance(raw, bool),
             "number": isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw),
             "boolean": isinstance(raw, bool), "object": isinstance(raw, dict), "array": isinstance(raw, list), "json": True}
    kinds = kind if isinstance(kind, list) else [kind]
    if not any((raw is None if k == "null" else valid.get(k, True)) for k in kinds):
        raise WorkbenchError(f"expected {kind}")
    if "enum" in spec and raw not in spec["enum"]:
        raise WorkbenchError("value is outside the declared enum")
    if spec.get("schema") is not None:
        validate_schema(raw, spec["schema"])
    return raw


def prepare(endpoint: Endpoint, values: dict, body=None, headers=None):
    if not endpoint.method or not endpoint.path:
        raise WorkbenchError("action has no declared HTTP mapping")
    unknown = set(values) - set(endpoint.parameters)
    if unknown:
        raise WorkbenchError(f"undeclared parameters: {sorted(unknown)}")
    values = dict(values)
    if endpoint.action_body and body is not None:
        if not isinstance(body, dict):
            raise WorkbenchError("catalog request body must be an object")
        allowed = {n for n, s in endpoint.parameters.items() if s["in"] == "body"}
        if set(body) - allowed - {"action"}:
            raise WorkbenchError("body contains undeclared fields or path parameters")
        if "action" in body and body["action"] != endpoint.id.rsplit(".", 1)[-1]:
            raise WorkbenchError("body action does not match selected endpoint")
        values.update({k: v for k, v in body.items() if k != "action"})
    path, query, request_headers = endpoint.path, [], dict(headers or {})
    request_body = {} if endpoint.action_body and endpoint.method != "GET" else body
    for name, spec in endpoint.parameters.items():
        if name not in values:
            if "default" in spec:
                values[name] = spec["default"]
            elif spec.get("required"):
                raise WorkbenchError(f"missing required parameter: {name}")
            else:
                continue
        value = coerce(values[name], spec)
        location = spec["in"]
        if location == "path":
            if value == "" or str(value) in {".", ".."}:
                raise WorkbenchError(f"invalid path parameter: {name}")
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        elif location == "query":
            if endpoint.action_body and isinstance(value, (dict, list, bool)):
                value = json.dumps(value, separators=(",", ":"))
            query.append((name, value))
        elif location == "header":
            if name.lower() in {"host", "content-length", "transfer-encoding", "connection"}:
                raise WorkbenchError("transport-controlled header")
            request_headers[name] = str(value)
        elif location == "body":
            request_body[name] = value
    if endpoint.action_body and endpoint.method != "GET":
        request_body["action"] = endpoint.id.rsplit(".", 1)[-1]
    if endpoint.body_required and request_body is None:
        raise WorkbenchError("request body is required")
    if request_body is not None:
        validate_schema(request_body, endpoint.request_schema)
    if query:
        path += "?" + urlencode(query, doseq=True)
    return path, request_body, request_headers


@dataclass(frozen=True)
class Policy:
    allow_mutations: bool = False
    allow_hardware: bool = False

    def check(self, endpoint: Endpoint, *, fixture: bool, confirmation: str):
        if endpoint.status in {"planned", "superseded"}:
            raise WorkbenchError(f"{endpoint.status} action cannot execute")
        if endpoint.effect == "read":
            return
        if not fixture:
            if not self.allow_mutations:
                raise WorkbenchError("SAFE mode blocks writes; restart with --allow-mutations")
            if endpoint.effect in {"hardware", "unknown"} and not self.allow_hardware:
                raise WorkbenchError("potential physical actuation requires --allow-hardware")
        if confirmation != endpoint.id:
            raise WorkbenchError("confirm this request by typing its complete action ID")


@dataclass
class Exchange:
    action_id: str
    method: str
    url: str
    request_body: object
    request_headers: dict
    status: int | None
    elapsed_ms: float
    size: int
    raw: str
    response_headers: dict
    contract: dict
    checks: list[dict] = field(default_factory=list)
    error: str | None = None
    timestamp: float = field(default_factory=time.time)


class Session:
    def __init__(self, client=None, policy=Policy(), history_limit=100):
        self.client, self.policy = client, policy
        self.history = deque(maxlen=history_limit)

    def execute(self, endpoint, values, body=None, headers=None, confirmation="", expected_status=None, latency_ms=None):
        if self.client is None:
            raise WorkbenchError("offline catalog mode: select a server or fixture first")
        self.policy.check(endpoint, fixture=isinstance(self.client, FixtureClient), confirmation=confirmation)
        path, body, headers = prepare(endpoint, values, body, headers)
        all_headers = {**self.client.headers, **headers}
        secrets = secret_values(all_headers) + secret_values(values) + secret_values(body)
        start = time.monotonic()
        status, response_headers, raw, error = None, {}, b"", None
        try:
            status, response_headers, raw = self.client.send(endpoint.method, path, body, headers)
        except WorkbenchError as exc:
            error = str(exc)
        elapsed = (time.monotonic() - start) * 1000
        checks = [{"check": "response received", "passed": status is not None}]
        decoded = None
        try:
            decoded = json.loads(raw)
            json_ok = True
        except (ValueError, UnicodeError):
            json_ok = False
        if status is not None:
            expected = expected_status if expected_status is not None else None
            accepted = status == expected if expected is not None else 200 <= status < 300
            checks.append({"check": f"status {expected if expected is not None else '2xx'}", "passed": accepted})
            checks.append({"check": "JSON decodes", "passed": json_ok})
            schema = endpoint.responses.get(str(status), endpoint.responses.get(str(status)[0] + "XX", endpoint.responses.get("default")))
            if endpoint.responses and str(status) not in endpoint.responses and str(status)[0] + "XX" not in endpoint.responses and "default" not in endpoint.responses:
                checks.append({"check": "declared response status", "passed": False})
            if schema is not None:
                try:
                    validate_schema(decoded, schema)
                    checks.append({"check": "response schema", "passed": json_ok})
                except WorkbenchError as exc:
                    checks.append({"check": "response schema", "passed": False, "detail": str(exc)})
            else:
                checks.append({"check": "response schema", "passed": None, "detail": "not declared"})
        if latency_ms is not None:
            checks.append({"check": f"latency <= {latency_ms} ms", "passed": elapsed <= latency_ms})
        secrets += secret_values(response_headers) + secret_values(decoded)
        # Unstructured bodies may contain arbitrary secrets. Keep them out of
        # history/export; JSON RAW is reserialized after structural redaction.
        safe_raw = json.dumps(redact(decoded, secrets), indent=2, ensure_ascii=False) if json_ok else "<non-JSON body withheld from history>"
        exchange = Exchange(endpoint.id, endpoint.method, redact(self.client.base_url + path, secrets),
            redact(body, secrets), redact(all_headers, secrets), status, elapsed, len(raw), safe_raw,
            redact(response_headers, secrets), redact(endpoint.contract(), secrets), checks, error)
        self.history.append(exchange)
        return exchange


def response_diff(previous: Exchange, current: Exchange) -> str:
    return "\n".join(difflib.unified_diff(previous.raw.splitlines(), current.raw.splitlines(), fromfile=previous.action_id, tofile=current.action_id, lineterm=""))


def export_request(exchange: Exchange, language="curl") -> str:
    if language == "python":
        return ("import json\nfrom urllib.request import Request, urlopen\n\n"
                f"request = Request({exchange.url!r}, method={exchange.method!r},\n"
                f"    headers={exchange.request_headers!r},\n"
                f"    data={'None' if exchange.request_body is None else 'json.dumps(' + repr(exchange.request_body) + ').encode()'})\n"
                "with urlopen(request, timeout=10) as response:\n    print(response.read().decode())\n")
    args = ["curl", "--max-time", "10", "-X", exchange.method]
    for key, value in exchange.request_headers.items():
        args += ["-H", f"{key}: {value}"]
    if exchange.request_body is not None:
        args += ["--data-raw", json.dumps(exchange.request_body)]
    return shlex.join([*args, exchange.url])
