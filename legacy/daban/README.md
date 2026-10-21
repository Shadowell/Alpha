# legacy/daban — 旧版打板策略脚本（归档）

本目录由 `strategy/daban/` 迁移而来（`git mv` 保留文件历史），**不属于运行时路径**，
没有任何模块 import 或调用这些脚本。

- `daban.py`、`daban_2.py`、`daban_3.py`：旧版打板/强势股筛选脚本，依赖手工运行与外部飞书 Webhook。
- 现行打板策略见 `strategy/first_limit_alpha/` 与策略中心页面。
- 归档原因：避免与活代码混淆、降低仓库搜索噪音（issue #20）。
- 如需恢复使用，请先评估其数据源、配置与当前架构的兼容性，再迁回并接入测试；不要直接在
  Web 服务进程中导入。

迁移记录见项目根目录 `CHANGELOG.md`。
