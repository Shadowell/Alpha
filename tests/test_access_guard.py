"""写操作访问控制回归测试（#19：常量时间令牌比较）。"""
from __future__ import annotations

import pytest

from app.services import access_guard
from app.services.access_guard import LOOPBACK_HOSTS, write_access_decision


def _decision(
    *,
    method: str = "POST",
    client_host: str | None = "127.0.0.1",
    header_token: str | None = None,
    configured_token: str | None = "secret-token",
    trust_edge: bool | None = False,
) -> str | None:
    return write_access_decision(
        method=method,
        client_host=client_host,
        header_token=header_token,
        configured_token=configured_token,
        trust_edge=trust_edge,
    )


# ── 读请求不受影响 ────────────────────────────────────────────


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_read_methods_are_always_allowed(method: str) -> None:
    assert _decision(method=method, client_host="8.8.8.8", header_token="wrong") is None


# ── token 模式：正确 / 错误 / 缺失 ────────────────────────────


def test_correct_token_is_allowed() -> None:
    assert _decision(header_token="secret-token", client_host="8.8.8.8") is None


def test_wrong_token_is_rejected() -> None:
    assert _decision(header_token="secret-tokeN", client_host="8.8.8.8") == "token_mismatch"


def test_missing_token_is_rejected() -> None:
    assert _decision(header_token=None, client_host="127.0.0.1") == "token_mismatch"
    assert _decision(header_token="", client_host="127.0.0.1") == "token_mismatch"
    assert _decision(header_token="   ", client_host="127.0.0.1") == "token_mismatch"


def test_token_whitespace_is_stripped_on_both_sides() -> None:
    assert _decision(header_token="  secret-token\n", configured_token=" secret-token ") is None


def test_token_from_environment_is_used_when_not_passed(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_WRITE_TOKEN", "env-token")
    assert (
        write_access_decision(
            method="PATCH",
            client_host="8.8.8.8",
            header_token="env-token",
            configured_token=None,
            trust_edge=False,
        )
        is None
    )
    assert (
        write_access_decision(
            method="PATCH",
            client_host="8.8.8.8",
            header_token="other",
            configured_token=None,
            trust_edge=False,
        )
        == "token_mismatch"
    )


def test_explicit_empty_configured_token_ignores_environment(monkeypatch) -> None:
    """configured_token="" 表示显式无 token，不应回退到环境变量。"""
    monkeypatch.setenv("ALPHA_WRITE_TOKEN", "env-token")
    assert _decision(header_token=None, configured_token="", client_host="127.0.0.1") is None
    # 显式空 token 时走回环/TRUST_EDGE 判定，而不是 token 校验。
    assert (
        _decision(header_token="env-token", configured_token="", client_host="8.8.8.8")
        == "non_loopback_without_token"
    )


# ── 非 ASCII token：必须走 bytes 比较，不能抛 TypeError ───────


@pytest.mark.parametrize(
    "token",
    ["秘密令牌", "令牌-α", "パスワード", "emoji-🔐-token"],
)
def test_non_ascii_token_compares_without_type_error(token: str) -> None:
    assert _decision(header_token=token, configured_token=token) is None
    assert _decision(header_token=token + "x", configured_token=token) == "token_mismatch"
    assert _decision(header_token="wrong", configured_token=token) == "token_mismatch"


# ── 常量时间比较实现约束 ──────────────────────────────────────


def test_token_comparison_uses_hmac_compare_digest_with_utf8_bytes(monkeypatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = access_guard.hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real_compare_digest(left, right)

    monkeypatch.setattr(access_guard.hmac, "compare_digest", spy)

    assert _decision(header_token="令牌-α", configured_token="令牌-α") is None
    assert calls == [("令牌-α".encode("utf-8"), "令牌-α".encode("utf-8"))]


def test_token_comparison_rejects_via_compare_digest(monkeypatch) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = access_guard.hmac.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real_compare_digest(left, right)

    monkeypatch.setattr(access_guard.hmac, "compare_digest", spy)

    assert _decision(header_token="bad", configured_token="good") == "token_mismatch"
    assert calls == [(b"bad", b"good")]


# ── TRUST_EDGE / 回环语义保持不变 ─────────────────────────────


@pytest.mark.parametrize("host", sorted(LOOPBACK_HOSTS))
def test_loopback_without_token_is_allowed(host: str) -> None:
    assert _decision(client_host=host, header_token=None, configured_token="") is None


def test_non_loopback_without_token_is_rejected() -> None:
    assert (
        _decision(client_host="192.168.1.50", header_token=None, configured_token="")
        == "non_loopback_without_token"
    )
    assert (
        _decision(client_host=None, header_token=None, configured_token="")
        == "non_loopback_without_token"
    )


def test_trust_edge_allows_non_loopback_without_token() -> None:
    assert _decision(client_host="172.18.0.1", configured_token="", trust_edge=True) is None


def test_token_takes_precedence_over_trust_edge() -> None:
    """配置 token 后即使 trust_edge=True，错误/缺失 token 仍必须拒绝。"""
    assert (
        _decision(
            client_host="172.18.0.1",
            header_token=None,
            configured_token="secret-token",
            trust_edge=True,
        )
        == "token_mismatch"
    )
    assert (
        _decision(
            client_host="172.18.0.1",
            header_token="wrong",
            configured_token="secret-token",
            trust_edge=True,
        )
        == "token_mismatch"
    )
    assert (
        _decision(
            client_host="172.18.0.1",
            header_token="secret-token",
            configured_token="secret-token",
            trust_edge=True,
        )
        is None
    )


@pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
def test_trust_edge_is_read_from_environment(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("ALPHA_TRUST_EDGE", raw)
    assert _decision(client_host="172.18.0.1", configured_token="", trust_edge=None) is None


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_trust_edge_disabled_from_environment(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("ALPHA_TRUST_EDGE", raw)
    assert (
        _decision(client_host="172.18.0.1", configured_token="", trust_edge=None)
        == "non_loopback_without_token"
    )


def test_explicit_trust_edge_overrides_environment(monkeypatch) -> None:
    monkeypatch.setenv("ALPHA_TRUST_EDGE", "1")
    assert (
        _decision(client_host="172.18.0.1", configured_token="", trust_edge=False)
        == "non_loopback_without_token"
    )
