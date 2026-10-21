"""Deployment-time security guard for the Alpha API.

The API has two write-protection modes (see ``app.services.access_guard``):
a shared ``ALPHA_WRITE_TOKEN``, or "loopback only" when no token is set.
Reverse-proxy / port-publishing deployments (docker compose, K8s, ...) hide the
real client address, so ``ALPHA_TRUST_EDGE=1`` was added to trust the edge
port binding instead of the peer address.

That trust flag is only safe while the edge really is private.  This module
turns the implicit assumption into an explicit, testable startup check:

* ``ALPHA_EDGE_EXPOSURE=public`` + ``ALPHA_TRUST_EDGE=1`` + no token
  -> fail-fast (``RuntimeError``); writes would otherwise be open to anyone
  who can reach the port.
* ``ALPHA_TRUST_EDGE=1`` + no token (even on loopback) -> prominent WARNING:
  the port binding is the only access boundary.
* non-loopback ``HOST`` + no token + no trust edge -> WARNING: ``access_guard``
  will reject remote writes (only loopback clients may write).
* ``ALPHA_EDGE_EXPOSURE`` is ``loopback`` by default; unknown values are
  treated as ``public`` (fail closed) with an explicit warning.

The functions are pure/parameterised so they can be unit-tested without
touching process environment; the application lifespan only needs to call
``enforce_deployment_security()`` once at startup.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "[::1]", "localhost"})
EXPOSURE_LOOPBACK = "loopback"
EXPOSURE_PUBLIC = "public"
_VALID_EXPOSURES = frozenset({EXPOSURE_LOOPBACK, EXPOSURE_PUBLIC})

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"", "0", "false", "no", "off"})

_WARNING_PREFIX = "SECURITY WARNING"


@dataclass(frozen=True)
class DeploymentSecurityReport:
    """Result of evaluating the deployment security posture."""

    host: str
    exposure: str
    exposure_explicit: bool
    trust_edge: bool
    token_configured: bool
    host_is_loopback: bool
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


def _normalize_exposure(raw: str | None) -> tuple[str, bool, str | None]:
    """Return ``(exposure, was_explicit, warning)``.

    Unset means ``loopback`` (the safe default matching compose).  Anything
    that is not literally ``loopback``/``public`` is treated as ``public`` so
    a typo cannot silently disable the token requirement.
    """

    if raw is None:
        return EXPOSURE_LOOPBACK, False, None
    value = raw.strip().lower()
    if not value:
        return EXPOSURE_LOOPBACK, False, None
    if value in _VALID_EXPOSURES:
        return value, True, None
    return (
        EXPOSURE_PUBLIC,
        True,
        (
            f"Unrecognised ALPHA_EDGE_EXPOSURE={raw!r}; treating it as "
            f"{EXPOSURE_PUBLIC!r} (allowed values: loopback|public)."
        ),
    )


def _env_flag(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in _TRUTHY


def evaluate_deployment_security(
    *,
    host: str | None = None,
    exposure: str | None = None,
    trust_edge: bool | None = None,
    write_token: str | None = None,
) -> DeploymentSecurityReport:
    """Evaluate the deployment security posture without raising.

    ``None`` for an argument means "read the corresponding environment
    variable"; explicit empty strings mean "configured as empty".
    """

    resolved_host = (
        host if host is not None else os.getenv("HOST", "127.0.0.1")
    ).strip()
    if not resolved_host:
        resolved_host = "127.0.0.1"

    raw_exposure = (
        exposure if exposure is not None else os.getenv("ALPHA_EDGE_EXPOSURE")
    )
    resolved_exposure, exposure_explicit, exposure_warning = _normalize_exposure(
        raw_exposure
    )

    resolved_trust_edge = (
        trust_edge
        if trust_edge is not None
        else _env_flag(os.getenv("ALPHA_TRUST_EDGE"))
    )
    resolved_token = (
        write_token if write_token is not None else os.getenv("ALPHA_WRITE_TOKEN", "")
    ).strip()
    token_configured = bool(resolved_token)
    host_is_loopback = resolved_host.lower() in LOOPBACK_HOSTS

    warnings: list[str] = []
    errors: list[str] = []

    if exposure_warning:
        warnings.append(exposure_warning)

    if (
        resolved_exposure == EXPOSURE_PUBLIC
        and resolved_trust_edge
        and not token_configured
    ):
        errors.append(
            "ALPHA_EDGE_EXPOSURE=public with ALPHA_TRUST_EDGE=1 and no "
            "ALPHA_WRITE_TOKEN. Every write request (paper trades, strategy "
            "edits, config changes, background jobs) would be accepted from "
            "any host that can reach the published port. Fix one of: "
            "(1) set ALPHA_WRITE_TOKEN to a long random secret and send it as "
            "the X-Alpha-Token header; (2) set ALPHA_EDGE_EXPOSURE=loopback "
            "and publish the port as 127.0.0.1:18888:18888 (compose default)."
        )

    if resolved_trust_edge and not token_configured:
        warnings.append(
            "ALPHA_TRUST_EDGE=1 without ALPHA_WRITE_TOKEN: write access is "
            "trusted for every client that reaches the API. The port binding "
            "(expected 127.0.0.1:18888) is the only boundary; never publish "
            "the port on a LAN/public interface in this mode. Configure "
            "ALPHA_WRITE_TOKEN (clients send X-Alpha-Token) or unset "
            "ALPHA_TRUST_EDGE."
        )

    if not host_is_loopback and not token_configured and not resolved_trust_edge:
        warnings.append(
            f"HOST={resolved_host!r} is not loopback while ALPHA_WRITE_TOKEN "
            "is unset and ALPHA_TRUST_EDGE is not enabled: access_guard will "
            "reject write requests from non-loopback clients. Set "
            "ALPHA_WRITE_TOKEN if remote writes are intended."
        )

    return DeploymentSecurityReport(
        host=resolved_host,
        exposure=resolved_exposure,
        exposure_explicit=exposure_explicit,
        trust_edge=resolved_trust_edge,
        token_configured=token_configured,
        host_is_loopback=host_is_loopback,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def enforce_deployment_security(
    *,
    logger: logging.Logger | None = None,
) -> DeploymentSecurityReport:
    """Log warnings and raise ``RuntimeError`` when the posture is unsafe.

    Intended to be called once from the FastAPI lifespan so a misconfigured
    public deployment fails at startup instead of exposing writes silently.
    """

    active_logger = logger or log
    report = evaluate_deployment_security()

    for warning in report.warnings:
        active_logger.warning("%s: %s", _WARNING_PREFIX, warning)

    if report.errors:
        detail = " ".join(report.errors)
        active_logger.error("%s: %s", _WARNING_PREFIX, detail)
        raise RuntimeError(f"Deployment security check failed. {detail}")

    return report
