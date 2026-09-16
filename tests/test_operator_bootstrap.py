from __future__ import annotations

import json

from metactl_transport import TransportError, resolve_operator_target
from doctor import doctor_report


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
