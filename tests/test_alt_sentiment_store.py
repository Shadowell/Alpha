"""另类数据落库单测（issue #33）：幂等 upsert、可删除、覆盖率、审计。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.services.alt_sentiment.factors import AltFactorBuilder
from app.services.alt_sentiment.normalize import normalize_attention_frame
from app.services.alt_sentiment.records import (
    SOURCE_ATTENTION,
    SOURCE_INTERACTIVE_QA,
    AltRecord,
    snapshot_visible_time,
)
from app.services.alt_sentiment.store import AltSentimentStore
import pandas as pd

AS_OF = snapshot_visible_time("2026-03-06")


@pytest.fixture()
def store(tmp_path) -> AltSentimentStore:
    return AltSentimentStore(tmp_path / "alt_sentiment.db")


def _attention_record(day: str, index: float) -> AltRecord:
    as_of = snapshot_visible_time(day)
    return AltRecord(
        source=SOURCE_ATTENTION,
        symbol="600000",
        event_time=as_of,
        as_of_time=as_of,
        metrics={
            "attention_index": index,
            "attention_score": 3.0,
            "attention_rank": 10.0,
            "attention_rank_delta": 1.0,
            "institution_ratio": None,
        },
        meta={"trade_date": day, "kind": "daily_snapshot"},
    )


def test_store_creates_schema_and_is_reentrant(tmp_path) -> None:
    path = tmp_path / "alt.db"
    first = AltSentimentStore(path)
    assert first.db_file == path
    second = AltSentimentStore(path)  # 重复构造不得报错
    assert second.stats()["daily_rows"] == 0


def test_upsert_records_is_idempotent(store: AltSentimentStore) -> None:
    records = [
        _attention_record("2026-03-05", 100.0),
        _attention_record("2026-03-06", 150.0),
    ]
    assert store.upsert_records(records)["inserted"] == 2
    again = store.upsert_records(records)
    assert again == {"inserted": 0, "skipped": 2}
    by_source = {row["source"]: row["count"] for row in store.stats()["raw_by_source"]}
    assert by_source[SOURCE_ATTENTION] == 2


def test_same_day_resync_does_not_duplicate_rows(store: AltSentimentStore) -> None:
    store.upsert_records([_attention_record("2026-03-06", 150.0)])
    store.upsert_records([_attention_record("2026-03-06", 151.0)])
    # content_hash 只与 (source, symbol, external_id) 有关，同交易日重复同步不新增行
    assert store.stats()["raw_by_source"][0]["count"] == 1


def test_load_records_respects_point_in_time_cutoff(store: AltSentimentStore) -> None:
    store.upsert_records(
        [_attention_record("2026-03-05", 100.0), _attention_record("2026-03-06", 150.0)]
    )
    visible = store.load_records(
        symbol="600000", until_as_of=datetime.fromisoformat("2026-03-05T15:30:00+08:00")
    )
    assert [r.trade_date for r in visible] == ["2026-03-05"]
    assert visible[0].metrics["attention_index"] == pytest.approx(100.0)


def test_delete_records_requires_filter(store: AltSentimentStore) -> None:
    store.upsert_records([_attention_record("2026-03-06", 150.0)])
    with pytest.raises(ValueError):
        store.delete_records()
    assert store.delete_records(symbol="600000", source=SOURCE_ATTENTION) == 1
    assert store.stats()["raw_by_source"] == []


def test_delete_records_by_date_range(store: AltSentimentStore) -> None:
    store.upsert_records(
        [_attention_record("2026-03-05", 100.0), _attention_record("2026-03-06", 150.0)]
    )
    assert store.delete_records(before_date="2026-03-06") == 1
    assert [r.trade_date for r in store.load_records()] == ["2026-03-06"]


def test_daily_upsert_is_idempotent_and_keeps_first_seen(
    store: AltSentimentStore,
) -> None:
    builder = AltFactorBuilder()
    store.upsert_records(
        [_attention_record("2026-03-05", 100.0), _attention_record("2026-03-06", 150.0)]
    )
    records = store.load_records(symbol="600000")
    row = builder.build("600000", AS_OF, records)
    assert store.upsert_daily([row]) == 1
    store.upsert_daily([builder.build("600000", AS_OF, records)])
    loaded = store.load_daily(symbol="600000")
    assert len(loaded) == 1
    assert loaded[0].as_of_time == AS_OF
    assert loaded[0].factor("attention_index") == pytest.approx(150.0)
    assert loaded[0].factor("alt_attention_score") is not None
    assert store.stats()["daily_rows"] == 1


def test_daily_missing_factor_roundtrips_as_missing(store: AltSentimentStore) -> None:
    builder = AltFactorBuilder()
    row = builder.build("600000", AS_OF, [_attention_record("2026-03-06", 150.0)])
    store.upsert_daily([row])
    loaded = store.load_daily(symbol="600000")[0]
    assert loaded.factor("interactive_qa_count_7d") is None
    assert loaded.availability["interactive_qa_count_7d"] is False
    assert loaded.factor("search_index") is None


def test_coverage_ratio_for_candidates(store: AltSentimentStore) -> None:
    builder = AltFactorBuilder()
    symbols = ["600000", "600001", "600002", "600003", "600004"]
    covered = symbols[:4]  # 4/5 = 80%
    rows = []
    records = []
    for symbol in covered:
        frame = pd.DataFrame(
            [
                {
                    "代码": symbol,
                    "交易日": "2026-03-06",
                    "关注指数": 120.0,
                    "综合得分": 3.0,
                    "目前排名": 10,
                    "上升": 1,
                }
            ]
        )
        records.extend(normalize_attention_frame(frame))
    store.upsert_records(records)
    for symbol in covered:
        rows.append(builder.build(symbol, AS_OF, store.load_records(symbol=symbol)))
    store.upsert_daily(rows)
    report = store.coverage("2026-03-06", symbols)
    assert report["total"] == 5
    assert report["covered"] == 4
    assert report["ratio"] == pytest.approx(0.8)
    assert report["missing_symbols"] == ["600004"]


def test_audit_rows_never_expose_text(store: AltSentimentStore) -> None:
    store.upsert_records(
        [
            AltRecord(
                source=SOURCE_INTERACTIVE_QA,
                symbol="002594",
                event_time=AS_OF - timedelta(hours=1),
                as_of_time=AS_OF - timedelta(hours=1),
                title="涉密问题正文",
                text="包含个人手机号 13800000000 的提问",
                external_id="q-1",
            )
        ]
    )
    rows = store.record_audit(symbol="002594")
    assert len(rows) == 1
    blob = str(rows)
    assert "涉密问题正文" not in blob
    assert "13800000000" not in blob


def test_corrupt_json_columns_fall_back_to_empty(store: AltSentimentStore) -> None:
    store.upsert_records([_attention_record("2026-03-06", 150.0)])
    import sqlite3

    conn = sqlite3.connect(str(store.db_file))
    conn.execute("UPDATE alt_sentiment_raw SET metrics_json = '{broken'")
    conn.commit()
    conn.close()
    record = store.load_records()[0]
    assert record.metrics == {}


def test_checkpoint_does_not_raise(store: AltSentimentStore) -> None:
    store.upsert_records([_attention_record("2026-03-06", 150.0)])
    store.checkpoint()
