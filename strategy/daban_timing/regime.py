"""PIT 安全的 regime 取数层（issue #32「严格 PIT」要求）。

盘后与盘中用的是**两份不同的数据**，本模块负责把它们隔开：

* 收盘标签（``regime`` / ``sentiment_score``）由 #25 在盘后生成，只有当
  ``decision_at`` 晚于 ``trade_date`` 的收盘可用时刻（默认 15:05 东八区）才可
  用于仓位决策；``date`` 标签来自未来（``trade_date > decision_at`` 的日期）
  一律视为不可用，绝不"偷看明天"。
* 盘中只能用 ``intraday_*`` 估算字段（``intraday_regime`` /
  ``intraday_sentiment_score``）；收盘分支会把所有 ``intraday_`` 前缀键剥掉，
  与 #25 的 ``RULE_INPUT_FIELDS`` 白名单形成双向隔离（测试锁定）。
* 任一分支拿不到标签时返回 ``source="missing"`` + ``missing`` 原因清单，
  而不是把缺失当成"冰点/0 分"。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

CLOSE_PREFIX_BLACKLIST = "intraday_"
CLOSE_REGIME_KEY = "regime"
CLOSE_SCORE_KEY = "sentiment_score"
INTRADAY_REGIME_KEY = "intraday_regime"
INTRADAY_SCORE_KEY = "intraday_sentiment_score"

#: 东八区固定偏移（与 ``app/services/time_utils`` 的 CN 口径一致，
#: 这里不导入 app 以便策略层独立可测）。
CN_TZ = timezone(timedelta(hours=8))

VALID_MODES: tuple[str, ...] = ("auto", "close", "intraday")


@dataclass(frozen=True)
class RegimeSnapshot:
    """决策时点可见的 regime 视图（唯一合法的情绪输入载体）。"""

    trade_date: str
    regime: str | None
    score: float | None
    source: str  # "close" | "intraday" | "missing"
    decision_at: str
    mode: str
    available: bool
    missing: tuple[str, ...] = ()
    prev_regime: str | None = None
    prev_score: float | None = None

    @property
    def is_resolved(self) -> bool:
        return self.regime is not None and self.available

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_date": self.trade_date,
            "regime": self.regime,
            "sentiment_score": self.score,
            "source": self.source,
            "decision_at": self.decision_at,
            "mode": self.mode,
            "available": self.available,
            "missing": list(self.missing),
            "prev_regime": self.prev_regime,
            "prev_sentiment_score": self.prev_score,
        }


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def parse_decision_at(value: str | datetime) -> datetime:
    """把决策时间解析成东八区 naive ``datetime``。

    支持 ``YYYY-MM-DD``、``YYYY-MM-DD HH:MM[:SS]``、ISO ``T`` 格式；带时区的
    输入会先折算到东八区再抹掉 tzinfo（后续一律按北京时间比较）。
    """

    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace("T", " ")
        if " " in text:
            date_part, time_part = text.split(" ", 1)
        else:
            date_part, time_part = text, "00:00"
        date_part = date_part.replace("/", "-").replace(".", "-")
        try:
            parsed_date = date.fromisoformat(date_part)
        except ValueError as exc:
            raise ValueError(f"unparsable decision date: {date_part!r}") from exc
        time_part = time_part.split("+")[0].split(".")[0].strip()
        try:
            hour, minute = (list(time_part.split(":")) + ["0", "0"])[:2]
            second = time_part.split(":")[2] if len(time_part.split(":")) > 2 else "0"
            clock = time(int(hour), int(minute), int(second))
        except (ValueError, IndexError) as exc:
            raise ValueError(f"unparsable decision time: {time_part!r}") from exc
        moment = datetime.combine(parsed_date, clock)
    if moment.tzinfo is not None:
        moment = moment.astimezone(CN_TZ).replace(tzinfo=None)
    return moment


def close_available_moment(trade_date: str, close_available_after: str) -> datetime:
    """收盘标签在该交易日的可用时刻（``trade_date + HH:MM``）。"""

    label = parse_decision_at(f"{trade_date} 00:00")
    hour, minute = (str(close_available_after).split(":") + ["0"])[:2]
    return label.replace(hour=int(hour), minute=int(minute))


def split_intraday(factors: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """把因子拆成 (收盘口径, 盘中估算) 两份，按键名前缀物理隔离。"""

    close: dict[str, Any] = {}
    intraday: dict[str, Any] = {}
    for key, value in dict(factors).items():
        name = str(key)
        if name.startswith(CLOSE_PREFIX_BLACKLIST):
            intraday[name] = value
        else:
            close[name] = value
    return close, intraday


def _miss(
    *,
    trade_date: str,
    decision_at: str,
    mode: str,
    reason: str,
    regime: str | None = None,
    score: float | None = None,
) -> RegimeSnapshot:
    return RegimeSnapshot(
        trade_date=trade_date,
        regime=regime,
        score=score,
        source="missing",
        decision_at=decision_at,
        mode=mode,
        available=False,
        missing=(reason,),
    )


def _close_snapshot(
    close_factors: Mapping[str, Any],
    *,
    trade_date: str,
    decision_moment: datetime,
    decision_at: str,
    mode: str,
    close_available_after: str,
) -> RegimeSnapshot | None:
    label_date = str(close_factors.get("trade_date") or trade_date)
    if label_date != trade_date:
        return _miss(
            trade_date=trade_date,
            decision_at=decision_at,
            mode=mode,
            reason=f"regime_label_date_mismatch:{label_date}",
        )
    label_day = parse_decision_at(f"{label_date} 00:00").date()
    if label_day > decision_moment.date():
        return _miss(
            trade_date=trade_date,
            decision_at=decision_at,
            mode=mode,
            reason="regime_label_from_future",
        )
    if decision_moment < close_available_moment(label_date, close_available_after):
        return None  # 标签尚未可用（盘中）
    regime = close_factors.get(CLOSE_REGIME_KEY)
    regime = str(regime) if regime not in (None, "") else None
    if regime is None:
        return _miss(
            trade_date=trade_date,
            decision_at=decision_at,
            mode=mode,
            reason="regime_label_missing",
        )
    score = _to_float(close_factors.get(CLOSE_SCORE_KEY))
    prev_regime = close_factors.get("prev_regime")
    return RegimeSnapshot(
        trade_date=trade_date,
        regime=regime,
        score=score,
        source="close",
        decision_at=decision_at,
        mode=mode,
        available=True,
        missing=() if score is not None else ("sentiment_score_missing",),
        prev_regime=None if prev_regime in (None, "") else str(prev_regime),
        prev_score=_to_float(close_factors.get("prev_sentiment_score")),
    )


def _intraday_snapshot(
    intraday_factors: Mapping[str, Any],
    *,
    trade_date: str,
    decision_at: str,
    mode: str,
) -> RegimeSnapshot | None:
    regime = intraday_factors.get(INTRADAY_REGIME_KEY)
    regime = str(regime) if regime not in (None, "") else None
    if regime is None:
        return None
    score = _to_float(intraday_factors.get(INTRADAY_SCORE_KEY))
    missing: tuple[str, ...] = () if score is not None else ("intraday_score_missing",)
    return RegimeSnapshot(
        trade_date=str(intraday_factors.get("trade_date") or trade_date),
        regime=regime,
        score=score,
        source="intraday",
        decision_at=decision_at,
        mode=mode,
        available=True,
        missing=missing,
    )


def resolve_regime(
    factors: Mapping[str, Any],
    *,
    trade_date: str,
    decision_at: str | datetime,
    mode: str = "auto",
    close_available_after: str = "15:05",
) -> RegimeSnapshot:
    """按决策时点解析可用 regime。

    ``mode="close"`` 只允许收盘标签；``"intraday"`` 只允许盘中估算；
    ``"auto"`` 先试收盘标签，标签不可用（盘中）时回落到 ``intraday_*``，
    并在 ``missing`` 中说明回落原因。
    """

    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")
    decision_text = (
        decision_at if isinstance(decision_at, str) else decision_at.isoformat(sep=" ")
    )
    moment = parse_decision_at(decision_at)
    close_factors, intraday_factors = split_intraday(factors)

    close_snapshot: RegimeSnapshot | None = None
    if mode in ("auto", "close"):
        close_snapshot = _close_snapshot(
            close_factors,
            trade_date=trade_date,
            decision_moment=moment,
            decision_at=decision_text,
            mode=mode,
            close_available_after=close_available_after,
        )
    if close_snapshot is not None and close_snapshot.available:
        return close_snapshot

    if mode == "close":
        if close_snapshot is not None:
            return close_snapshot
        return _miss(
            trade_date=trade_date,
            decision_at=decision_text,
            mode=mode,
            reason="regime_label_not_available_at_decision_time",
        )

    intraday = _intraday_snapshot(
        intraday_factors, trade_date=trade_date, decision_at=decision_text, mode=mode
    )
    if intraday is not None:
        if mode == "auto":
            reason = (
                "close_label_not_yet_available_use_intraday"
                if close_snapshot is None
                else f"close_{close_snapshot.missing[0]}_use_intraday"
            )
            return replace(
                intraday,
                missing=intraday.missing + (reason,),
            )
        return intraday

    if mode == "intraday":
        return _miss(
            trade_date=trade_date,
            decision_at=decision_text,
            mode=mode,
            reason="intraday_regime_missing",
        )
    if close_snapshot is not None:
        return close_snapshot
    return _miss(
        trade_date=trade_date,
        decision_at=decision_text,
        mode=mode,
        reason="no_regime_source_available",
    )
