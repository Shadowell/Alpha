from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.services.deployment_guard import (
    enforce_deployment_security,
    evaluate_deployment_security,
)

ROOT = Path(__file__).resolve().parents[1]


def test_public_trust_edge_without_token_is_a_hard_error():
    report = evaluate_deployment_security(
        host="0.0.0.0",
        exposure="public",
        trust_edge=True,
        write_token="",
    )

    assert report.ok is False
    assert report.exposure == "public"
    assert any("ALPHA_WRITE_TOKEN" in err for err in report.errors)
    assert any("ALPHA_EDGE_EXPOSURE=loopback" in err for err in report.errors)


def test_public_trust_edge_with_token_passes():
    report = evaluate_deployment_security(
        host="0.0.0.0",
        exposure="public",
        trust_edge=True,
        write_token="s3cret-token",
    )

    assert report.ok is True
    assert report.token_configured is True
    assert report.warnings == ()


def test_loopback_trust_edge_without_token_warns_about_port_binding():
    report = evaluate_deployment_security(
        host="127.0.0.1",
        exposure="loopback",
        trust_edge=True,
        write_token="",
    )

    assert report.ok is True
    assert any("port binding" in warning for warning in report.warnings)


def test_default_exposure_is_loopback(monkeypatch):
    monkeypatch.delenv("ALPHA_EDGE_EXPOSURE", raising=False)
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.delenv("ALPHA_TRUST_EDGE", raising=False)
    monkeypatch.delenv("ALPHA_WRITE_TOKEN", raising=False)

    report = evaluate_deployment_security()

    assert report.exposure == "loopback"
    assert report.exposure_explicit is False
    assert report.ok is True
    assert report.warnings == ()


def test_non_loopback_host_without_token_or_trust_edge_warns_about_access_guard():
    report = evaluate_deployment_security(
        host="0.0.0.0",
        exposure="loopback",
        trust_edge=False,
        write_token="",
    )

    assert report.ok is True
    assert any("access_guard" in warning for warning in report.warnings)
    assert any("HOST='0.0.0.0'" in warning for warning in report.warnings)


def test_unknown_exposure_fails_closed_to_public():
    report = evaluate_deployment_security(
        host="0.0.0.0",
        exposure="publik",
        trust_edge=True,
        write_token="",
    )

    assert report.exposure == "public"
    assert report.exposure_explicit is True
    assert report.ok is False
    assert any(
        "Unrecognised ALPHA_EDGE_EXPOSURE" in warning for warning in report.warnings
    )


def test_enforce_reads_environment_and_raises_on_public_unsafe(monkeypatch):
    monkeypatch.setenv("ALPHA_EDGE_EXPOSURE", "public")
    monkeypatch.setenv("ALPHA_TRUST_EDGE", "1")
    monkeypatch.delenv("ALPHA_WRITE_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="Deployment security check failed"):
        enforce_deployment_security()


def test_enforce_warns_on_trust_edge_without_token(monkeypatch, caplog):
    monkeypatch.setenv("ALPHA_EDGE_EXPOSURE", "loopback")
    monkeypatch.setenv("ALPHA_TRUST_EDGE", "true")
    monkeypatch.setenv("ALPHA_WRITE_TOKEN", "")

    with caplog.at_level(logging.WARNING, logger="app.services.deployment_guard"):
        report = enforce_deployment_security()

    assert report.ok is True
    assert any("SECURITY WARNING" in record.message for record in caplog.records)


def test_compose_defaults_are_documented_and_loopback_scoped():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert 'ALPHA_EDGE_EXPOSURE: "loopback"' in compose
    assert "http://127.0.0.1:18888/api/health" in compose
    assert "当前版本 API 无鉴权" not in compose
    assert "18888:18888" in compose
