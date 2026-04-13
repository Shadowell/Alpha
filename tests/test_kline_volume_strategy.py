import pandas as pd

from app.config import StrategyConfig
from app.services.strategy_engine import analyze_adjustment_candidate


def _make_hist(rows: int = 20, explode_last: bool = False) -> pd.DataFrame:
    highs = [10.2] * rows
    lows = [9.9] * rows
    opens = [10.0] * rows
    closes = [10.05] * rows
    volumes = [1200] * rows
    for i in range(rows - 10, rows - 5):
        volumes[i] = 500
    for i in range(rows - 5, rows):
        volumes[i] = 600
        highs[i] = 10.1
        lows[i] = 9.94
    if explode_last:
        highs[-1] = 11.2
        lows[-1] = 9.9
        opens[-1] = 10.0
        closes[-1] = 10.95
        volumes[-1] = 4000
    return pd.DataFrame(
        {
            "最高": highs,
            "最低": lows,
            "开盘": opens,
            "收盘": closes,
            "成交量": volumes,
            "成交额": [x * y for x, y in zip(volumes, closes)],
            "涨跌幅": [0.2] * rows,
        }
    )


def test_kline_volume_strategy_balanced_passes_base_pattern():
    stock = {"symbol": "000001", "name": "平安银行"}
    hist = _make_hist(explode_last=False)
    passed, reasons, metrics = analyze_adjustment_candidate(stock, hist, StrategyConfig())
    assert passed is True
    assert "缩量横盘收敛" in reasons
    assert metrics["chase_risk"] is False


def test_kline_volume_strategy_rejects_chase_risk():
    stock = {"symbol": "000001", "name": "平安银行"}
    hist = _make_hist(explode_last=True)
    passed, reasons, metrics = analyze_adjustment_candidate(stock, hist, StrategyConfig())
    assert passed is False
    assert metrics["chase_risk"] is True
    assert "末端爆量长阳，追高风险" in reasons
