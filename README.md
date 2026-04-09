# 漏斗选股系统

基于你现有 `daban` 脚本逻辑实现的 Web 版漏斗系统，支持：

- 盘后筛选调整期未突破股票（候选池）
- 次日 1 分钟实时评分
- 三池漏斗管理（候选池 / 重点池 / 买入池）
- 2-3 概念标签（按热度 Top3，红/橙/蓝）
- 热门概念大盘（涨幅、涨停数、上涨/下跌家数、领涨股、入选数量）
- WebSocket 实时推送更新

## 运行

```bash
pip3 install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 18888 --reload
```

或使用脚本管理服务：

```bash
./start.sh
./stop.sh
./restart.sh
```

可选环境变量：

- `PORT=18888`（默认 `18888`）
- `HOST=0.0.0.0`（默认 `0.0.0.0`）
- `RELOAD=0`（默认关闭，后台运行更稳定；如需热重载可设 `RELOAD=1`）
- 日志文件：`logs/server.log`

项目级规则：

- [AGENTS.md](/Users/jie.feng/wlb/Alpha/AGENTS.md)
- 约定每次修改后执行 `./restart.sh`

浏览器打开：

- http://127.0.0.1:18888

## 主要接口

- `GET /api/funnel?trade_date=YYYY-MM-DD`
- `GET /api/market/hot-concepts?trade_date=YYYY-MM-DD`
- `GET /api/market/hot-stocks?trade_date=YYYY-MM-DD`
- `GET /api/stock/{symbol}/detail?trade_date=YYYY-MM-DD&kline_days=30`
- `POST /api/pool/move`
- `POST /api/score/recompute`
- `POST /api/jobs/eod-screen`
- `WS /ws/realtime`

## 测试

```bash
pytest -q
```

## 说明

- 数据源：AkShare（沿用你脚本口径）
- 买入池上限：5只
- 自动降级：买入池个股分数连续 5 分钟 < 65 自动降至重点池
- 状态存储：SQLite `data/funnel_state.db`（首次启动可自动迁移旧 `data/funnel_state.json`）
