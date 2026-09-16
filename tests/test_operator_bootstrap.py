from __future__ import annotations

import json

import pytest

from metactl_transport import TransportError, resolve_operator_target
from doctor import doctor_report
from presentation import redact


def test_operator_target_prefers_metactl_url(monkeypatch):
    monkeypatch.setenv("META_WEBUI_METACTL_CENTRAL_URL", "http://central-a:18087")
    monkeypatch.setenv("META_WEBUI_EVOLVER_CONTROL_URL", "http://legacy:18087")

    resolved = resolve_operator_target()

    assert resolved.url == "http://central-a:18087"
    assert resolved.source == "META_WEBUI_METACTL_CENTRAL_URL"
    assert "secret-value" not in repr(resolved).lower()


def test_doctor_reports_live_discovery_and_catalog_drift_without_secrets():
    class FakeTransport:
        def discover_actions(self):
            return {"version": "2", "actions": [{"id": "evolver.edge.status"}]}

    report = doctor_report(
        transport=FakeTransport(),
        target=resolve_operator_target({"META_WEBUI_METACTL_CENTRAL_URL": "http://central.test"}),
        local_catalog={"version": "1", "api": {"evolver.edge.status": {}}},
    )

    assert report["target"] == {"url": "http://central.test", "source": "META_WEBUI_METACTL_CENTRAL_URL"}
    assert report["reachable"] is True
    assert report["auth"] == {"operator": False, "token": False, "shared_secret": False}
    assert report["discovery"] == {"status": "ok", "version": "2", "actions": 1}
    assert report["drift"]["status"] == "changed"
    assert "central.test" not in json.dumps(report["remediation"])


def test_doctor_redacts_target_url_credentials_and_sensitive_query_values():
    class FakeTransport:
        def discover_actions(self):
            return {"version": "2", "actions": []}

    report = doctor_report(
        transport=FakeTransport(),
        target=resolve_operator_target({
            "META_WEBUI_METACTL_CENTRAL_URL":
                "https://operator:password@central.test/api?token=secret&keep=yes&api_key=another",
        }),
        local_catalog={"version": "2", "api": {}},
    )

    assert report["target"] == {
        "url": "https://central.test/api?token=%3Credacted%3E&keep=yes&api_key=%3Credacted%3E",
        "source": "META_WEBUI_METACTL_CENTRAL_URL",
    }
    target_url = report["target"]["url"]
    assert "password" not in target_url
    assert "=secret" not in target_url
    assert "=another" not in target_url


def test_doctor_redacts_schemeless_url_userinfo_and_query_secrets():
    class FakeTransport:
        def discover_actions(self):
            return {"version": "2", "actions": []}

    report = doctor_report(
        transport=FakeTransport(),
        target=resolve_operator_target({
            "META_WEBUI_METACTL_CENTRAL_URL": "//operator:password@central.test/api?shared_secret=secret&keep=yes",
        }),
        local_catalog={"version": "2", "api": {}},
    )

    assert report["target"]["url"] == \
        "//central.test/api?shared_secret=%3Credacted%3E&keep=yes"
    target_url = report["target"]["url"]
    assert "password" not in target_url
    assert "=secret" not in target_url


@pytest.mark.parametrize("url", [
    "HTTPS://operator:password@central.test/api?TOKEN=secret&keep=yes",
    "http:///api?token=secret",
    "http://[bad/api?token=secret",
])
def test_doctor_redacts_uppercase_empty_authority_and_malformed_urls(url):
    class FakeTransport:
        def discover_actions(self):
            return {"version": "2", "actions": []}

    report = doctor_report(
        transport=FakeTransport(),
        target=resolve_operator_target({"META_WEBUI_METACTL_CENTRAL_URL": url}),
        local_catalog={"version": "2", "api": {}},
    )
    target_url = report["target"]["url"]
    assert "password" not in target_url
    assert "=secret" not in target_url
    assert report["target"]["url"] in {"https://central.test/api?TOKEN=%3Credacted%3E&keep=yes", "<redacted URL>"}


def test_doctor_is_json_clean_and_gives_remediation_for_unreachable_target():
    class FakeTransport:
        def discover_actions(self):
            raise TransportError("network_failure", "central control plane is unavailable")

    report = doctor_report(
        transport=FakeTransport(),
        target=resolve_operator_target({"META_WEBUI_METACTL_CENTRAL_URL": "http://central.test"}),
    )

    assert report["reachable"] is False
    assert report["discovery"]["status"] == "unavailable"
    assert report["drift"]["status"] == "not_checked"
    assert report["remediation"] == ["rtk tools/dev-env server network", "rtk tools/dev-env server exec metactl doctor"]
    assert "unavailable" in json.dumps(report)


@pytest.mark.parametrize("status,kind,remediation", [
    (401, "unauthorized", "META_WEBUI_METACTL_TOKEN"),
    (403, "forbidden", "permitted by the WebUI gateway"),
])
def test_doctor_preserves_auth_failure_class_and_safe_remediation(status, kind, remediation):
    class FakeTransport:
        def discover_actions(self):
            raise TransportError(kind, "secret must not be echoed", status=status)

    report = doctor_report(transport=FakeTransport(), target=resolve_operator_target({
        "META_WEBUI_METACTL_CENTRAL_URL": "https://gateway.test",
    }))

    assert report["discovery"] == {"status": "error", "kind": kind, "status_code": status}
    assert remediation in " ".join(report["remediation"])
    assert "secret must not" not in json.dumps(report)


def test_doctor_treats_unexpected_probe_failure_as_unavailable():
    class FakeTransport:
        def discover_actions(self):
            raise OSError("controller-first import leaked a private failure")

    report = doctor_report(
        transport=FakeTransport(),
        target=resolve_operator_target({"META_WEBUI_METACTL_CENTRAL_URL": "http://central.test"}),
    )

    assert report["reachable"] is False
    assert report["discovery"]["status"] == "unavailable"
    assert "controller-first" not in json.dumps(report)


def test_doctor_does_not_collapse_unexpected_non_network_failures():
    class FakeTransport:
        def discover_actions(self):
            raise RuntimeError("implementation bug")

    with pytest.raises(RuntimeError, match="implementation bug"):
        doctor_report(transport=FakeTransport(), target=resolve_operator_target({
            "META_WEBUI_METACTL_CENTRAL_URL": "http://central.test",
        }))


def test_presentation_redacts_nested_credentials_and_urls():
    value = redact({"outer": [{"access_token": "secret", "url": "//user:pw@central.test/?token=x"}],
                    "safe": "value"})
    assert value == {"outer": [{"access_token": "<redacted>",
                                 "url": "//central.test/?token=%3Credacted%3E"}],
                     "safe": "value"}


def test_doctor_distinguishes_malformed_discovery_from_network_failure():
    class FakeTransport:
        def discover_actions(self):
            return {"version": "1", "actions": {"not": "a list"}}

    report = doctor_report(transport=FakeTransport(), target=resolve_operator_target({
        "META_WEBUI_METACTL_CENTRAL_URL": "http://central.test",
    }))

    assert report["discovery"] == {"status": "error", "kind": "malformed_response"}
    assert "valid JSON" in " ".join(report["remediation"])
