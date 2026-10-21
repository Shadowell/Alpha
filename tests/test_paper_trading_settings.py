from __future__ import annotations

import math

import pytest

from app.services.paper_trading import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_MIN_COMMISSION,
    DEFAULT_SLIPPAGE_RATE,
    DEFAULT_STAMP_TAX_RATE,
    PaperTradingService,
)


@pytest.fixture()
def service(tmp_path) -> PaperTradingService:
    return PaperTradingService(tmp_path / "paper.db")


def test_defaults_are_unchanged(service):
    assert service.get_settings() == {
        "commission_rate": DEFAULT_COMMISSION_RATE,
        "min_commission": DEFAULT_MIN_COMMISSION,
        "stamp_tax_rate": DEFAULT_STAMP_TAX_RATE,
        "slippage_rate": DEFAULT_SLIPPAGE_RATE,
    }


def test_valid_boundaries_are_accepted(service):
    service.update_settings(
        commission_rate=1.0,
        min_commission=100.0,
        stamp_tax_rate=0.0,
        slippage_rate=0.1,
    )
    settings = service.get_settings()
    assert settings["commission_rate"] == 1.0
    assert settings["min_commission"] == 100.0
    assert settings["stamp_tax_rate"] == 0.0
    assert settings["slippage_rate"] == 0.1

    service.update_settings(
        commission_rate=0.0,
        min_commission=0.0,
        stamp_tax_rate=1.0,
        slippage_rate=0.0,
    )
    settings = service.get_settings()
    assert settings["commission_rate"] == 0.0
    assert settings["min_commission"] == 0.0
    assert settings["stamp_tax_rate"] == 1.0
    assert settings["slippage_rate"] == 0.0


@pytest.mark.parametrize(
    "name,value",
    [
        ("commission_rate", -0.0001),
        ("commission_rate", 1.0001),
        ("commission_rate", 10),
        ("min_commission", -1),
        ("min_commission", 100.01),
        ("stamp_tax_rate", -0.01),
        ("stamp_tax_rate", 2),
        ("slippage_rate", -0.0001),
        ("slippage_rate", 0.1001),
        ("slippage_rate", 1),
    ],
)
def test_out_of_range_values_are_rejected(service, name, value):
    before = service.get_settings()
    with pytest.raises(ValueError, match=name):
        service.update_settings(**{name: value})
    assert service.get_settings() == before


@pytest.mark.parametrize("value", ["abc", None, object()])
def test_non_numeric_values_are_rejected(service, value):
    with pytest.raises(ValueError, match="commission_rate"):
        service.update_settings(commission_rate=value)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_values_are_rejected(service, value):
    with pytest.raises(ValueError, match="commission_rate"):
        service.update_settings(commission_rate=value)


def test_invalid_field_rolls_back_the_whole_update(service):
    before = service.get_settings()

    with pytest.raises(ValueError, match="slippage_rate"):
        service.update_settings(commission_rate=0.001, slippage_rate=0.5)

    assert service.get_settings() == before


def test_valid_partial_update_only_changes_supplied_fields(service):
    service.update_settings(commission_rate=0.001)

    settings = service.get_settings()
    assert settings["commission_rate"] == pytest.approx(0.001)
    assert settings["min_commission"] == DEFAULT_MIN_COMMISSION
    assert settings["stamp_tax_rate"] == DEFAULT_STAMP_TAX_RATE
    assert settings["slippage_rate"] == DEFAULT_SLIPPAGE_RATE


def test_unknown_keys_are_ignored_for_backward_compatibility(service):
    service.update_settings(unknown_field=123, commission_rate=0.002)

    assert service.get_settings()["commission_rate"] == pytest.approx(0.002)
