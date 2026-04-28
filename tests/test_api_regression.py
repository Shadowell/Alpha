"""全站 API 回归测试 — 覆盖所有 read-only 端点 + 非破坏性 POST。

运行：
    cd Alpha
    python3 -m pytest tests/test_api_regression.py -v --tb=short

前提：后端必须已启动在 127.0.0.1:18888。
"""
from __future__ import annotations

import os
import pytest
import httpx

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"

BASE_URL = os.environ.get("ALPHA_TEST_BASE_URL", "http://127.0.0.1:18888")
TEST_SYMBOL = "600519"  # 贵州茅台，主板大盘股


@pytest.fixture(scope="module")
def client():
    transport = httpx.HTTPTransport(proxy=None)
    with httpx.Client(base_url=BASE_URL, timeout=60.0, transport=transport) as c:
        yield c


# ─────────────────────────────────────────────
# 1. 静态页面
# ─────────────────────────────────────────────

class TestStaticPages:
    def test_root_page(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "html" in r.headers.get("content-type", "").lower() or len(r.content) > 100

    def test_notice_page(self, client):
        r = client.get("/notice", follow_redirects=True)
        assert r.status_code == 200

    def test_static_app_js(self, client):
        r = client.get("/static/app.js")
        assert r.status_code == 200
        assert len(r.content) > 1000

    def test_static_styles_css(self, client):
        r = client.get("/static/styles.css")
        assert r.status_code == 200

    def test_static_index_html(self, client):
        r = client.get("/static/index.html")
        assert r.status_code == 200


# ─────────────────────────────────────────────
# 2. 大盘行情
# ─────────────────────────────────────────────

class TestMarket:
    def test_hot_concepts(self, client):
        r = client.get("/api/market/hot-concepts")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        # items 可能叫 concepts 或 items
        assert any(k in data for k in ("items", "concepts", "data"))

    def test_hot_stocks(self, client):
        r = client.get("/api/market/hot-stocks")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_stock_realtime(self, client):
        r = client.get(f"/api/stock/{TEST_SYMBOL}/realtime")
        assert r.status_code in (200, 404)  # 盘后/停牌时 404 可接受

    def test_stock_detail(self, client):
        r = client.get(f"/api/stock/{TEST_SYMBOL}/detail")
        assert r.status_code == 200
        data = r.json()
        assert "symbol" in data or "name" in data or "kline" in data

    def test_stock_detail_with_kline_days(self, client):
        r = client.get(f"/api/stock/{TEST_SYMBOL}/detail?kline_days=30")
        assert r.status_code == 200


# ─────────────────────────────────────────────
# 3. 策略漏斗
# ─────────────────────────────────────────────

class TestFunnel:
    def test_funnel_snapshot(self, client):
        r = client.get("/api/funnel")
        assert r.status_code == 200
        data = r.json()
        assert "pools" in data or "trade_date" in data

    def test_strategy_profile(self, client):
        r = client.get("/api/strategy/profile")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_first_limit_alpha_status(self, client):
        r = client.get("/api/strategy/first-limit-alpha/status")
        assert r.status_code == 200
        data = r.json()
        assert "artifact_root" in data

    def test_quiet_breakout_snapshot(self, client):
        r = client.get("/api/strategy/quiet-breakout")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)


# ─────────────────────────────────────────────
# 4. 预测选股 + Kronos
# ─────────────────────────────────────────────

class TestPredict:
    def test_predict_funnel(self, client):
        r = client.get("/api/predict-funnel")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_predict_funnel_config(self, client):
        r = client.get("/api/predict-funnel/config")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_predict_funnel_config_update(self, client):
        """读旧值 → 回写同值（非破坏性）"""
        r = client.get("/api/predict-funnel/config")
        cfg = r.json()
        # 只回写一个允许的字段
        payload = {"feishu_enabled": bool(cfg.get("feishu_enabled", False))}
        r2 = client.post("/api/predict-funnel/config", json=payload)
        assert r2.status_code == 200

    def test_kronos_predict(self, client):
        """Kronos 预测首次请求会加载模型，可能需要 30 秒。"""
        r = client.get(f"/api/predict/{TEST_SYMBOL}/kronos?lookback=30&horizon=3", timeout=120)
        # 若模型未配置，503 也可接受
        assert r.status_code in (200, 503)


class TestHotStockAI:
    def test_hot_stock_ai_snapshot(self, client):
        r = client.get("/api/strategy/hot-stock-ai")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert "pools" in data

    def test_hot_stock_ai_config(self, client):
        r = client.get("/api/strategy/hot-stock-ai/config")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)
        assert "top_n" in data
        assert "tradingagents_enabled" in data

    def test_hot_stock_ai_config_update(self, client):
        r = client.get("/api/strategy/hot-stock-ai/config")
        cfg = r.json()
        payload = {
            "auto_refresh_enabled": bool(cfg.get("auto_refresh_enabled", True)),
            "tradingagents_top_n": int(cfg.get("tradingagents_top_n", 3)),
        }
        r2 = client.post("/api/strategy/hot-stock-ai/config", json=payload)
        assert r2.status_code == 200

    def test_hot_stock_ai_pool_move_route(self, client):
        r = client.post(
            "/api/strategy/hot-stock-ai/pool/move",
            json={"symbol": "NO_SUCH_SYMBOL", "target_pool": "focus"},
        )
        assert r.status_code == 404


# ─────────────────────────────────────────────
# 5. K 线缓存
# ─────────────────────────────────────────────

class TestKlineCache:
    def test_kline_by_symbol(self, client):
        r = client.get(f"/api/kline/{TEST_SYMBOL}?days=30")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data or "kline" in data

    def test_kline_cache_stats(self, client):
        r = client.get("/api/jobs/kline-cache/stats")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_kline_cache_status(self, client):
        r = client.get("/api/jobs/kline-cache/status")
        assert r.status_code == 200

    def test_kline_cache_progress(self, client):
        r = client.get("/api/jobs/kline-cache/progress")
        assert r.status_code == 200

    def test_kline_cache_logs(self, client):
        r = client.get("/api/jobs/kline-cache/logs?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data or "logs" in data or isinstance(data, list)

    def test_kline_cache_report(self, client):
        r = client.get("/api/jobs/kline-cache/report")
        assert r.status_code == 200


# ─────────────────────────────────────────────
# 6. 公告
# ─────────────────────────────────────────────

class TestNotice:
    def test_notice_funnel(self, client):
        r = client.get("/api/notice/funnel")
        assert r.status_code == 200

    def test_notice_keywords(self, client):
        r = client.get("/api/notice/keywords")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)


# ─────────────────────────────────────────────
# 7. Hermes Agent
# ─────────────────────────────────────────────

class TestAgent:
    def test_agent_status(self, client):
        r = client.get("/api/agent/status")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_agent_tasks(self, client):
        r = client.get("/api/agent/tasks?limit=5")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data

    def test_monitor_config(self, client):
        r = client.get("/api/agent/monitor/config")
        assert r.status_code == 200

    def test_monitor_messages(self, client):
        r = client.get("/api/agent/monitor/messages?limit=5")
        assert r.status_code == 200


# ─────────────────────────────────────────────
# 8. 模拟盘
# ─────────────────────────────────────────────

class TestPaper:
    def test_positions(self, client):
        r = client.get("/api/paper/positions")
        assert r.status_code == 200

    def test_history(self, client):
        r = client.get("/api/paper/history")
        assert r.status_code == 200

    def test_summary(self, client):
        r = client.get("/api/paper/summary")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, dict)

    def test_trades(self, client):
        r = client.get("/api/paper/trades?limit=5")
        assert r.status_code == 200

    def test_settings(self, client):
        r = client.get("/api/paper/settings")
        assert r.status_code == 200


# ─────────────────────────────────────────────
# 9. 已移除提案 / Hermes AI 能力接口
# ─────────────────────────────────────────────

class TestRemovedProposalApis:
    @pytest.mark.parametrize("path,method", [
        ("/api/agent/proposals", "get"),
        ("/api/agent/proposals/stats", "get"),
        ("/api/agent/proposals/999999", "get"),
        ("/api/agent/proposals/999999/approve", "post"),
        ("/api/agent/proposals/999999/reject", "post"),
        ("/api/agent/proposals/batch", "post"),
        ("/api/agent/proposals/create", "post"),
        ("/api/hermes-ai/proposal-learner", "get"),
        ("/api/hermes-ai/risk", "get"),
        ("/api/hermes-ai/risk/config", "post"),
        ("/api/hermes-ai/risk/tick", "post"),
        ("/api/hermes-ai/auto-trade", "get"),
        ("/api/hermes-ai/auto-trade/config", "post"),
        ("/api/hermes-ai/auto-trade/tick", "post"),
        ("/api/hermes-ai/backtest", "get"),
        ("/api/hermes-ai/backtest/run", "post"),
        ("/api/hermes-ai/research/600519", "post"),
        ("/api/hermes-ai/news-insight", "get"),
        ("/api/hermes-ai/news-insight/run", "post"),
        ("/api/hermes-ai/weekly-report", "get"),
        ("/api/hermes-ai/weekly-report/run", "post"),
    ])
    def test_removed_routes_return_404(self, client, path, method):
        if method == "post":
            r = client.post(path, json={})
        else:
            r = client.get(path)
        assert r.status_code == 404


# ─────────────────────────────────────────────
# 10. 公告详情（基础可访问性）
# ─────────────────────────────────────────────

class TestNoticeDetail:
    def test_notice_detail_may_404(self, client):
        """公告详情：可能 200/404，不应 500"""
        r = client.get(f"/api/notice/{TEST_SYMBOL}/detail")
        assert r.status_code in (200, 404)


# ─────────────────────────────────────────────
# 12. 响应时延健康检查
# ─────────────────────────────────────────────

class TestPerformance:
    @pytest.mark.parametrize("path", [
        "/api/funnel",
        "/api/strategy/profile",
        "/api/agent/status",
        "/api/paper/summary",
        "/api/jobs/kline-cache/stats",
    ])
    def test_fast_endpoints_under_3s(self, client, path):
        """关键只读端点应在 3 秒内返回。"""
        import time
        t0 = time.time()
        r = client.get(path)
        elapsed = time.time() - t0
        assert r.status_code == 200, f"{path} 返回 {r.status_code}"
        assert elapsed < 3.0, f"{path} 耗时 {elapsed:.2f}s 超过 3s"
