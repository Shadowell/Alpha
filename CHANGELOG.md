# Changelog

本文件记录 Alpha 的重要版本变化。版本号遵循 [Semantic Versioning](https://semver.org/)。

## [Unreleased]

### Added

- Bilingual README overview (English + 中文) with contribution / security entry points.
- CONTRIBUTING.md, SECURITY.md, GitHub Issue templates, and PR template for OSS maintainership.

### Removed

- 移除未被调用的全表扫描快照入口 `FunnelService._pick_screen_snapshot` / `KlineCacheService.build_snapshot_for_screen`，避免误接入时对 500 万行级 K 线全表扫描（#20）。
- 旧版打板脚本 `strategy/daban/` 归档到 `legacy/daban/`（`git mv` 保留历史），运行时不再引用（#20）。

### Changed

- `quiet_breakout_scanner` 明确标注为兼容保留路径，删除前需同步 README 与前端（#20）。

## [0.1.0] - 2026-07-22

### Added

- 首次正式发布 Alpha 自进化量化选股系统。
- 提供 A 股三池漏斗选股、Kronos K 线预测、公告分析、热门股智能分析、Hermes Agent 监控和模拟交易能力。
- 增加标准 MIT 开源许可证与 GitHub Release 版本入口。

### Changed

- 将 README 中 TradingAgents 的本机文件链接替换为可公开访问的 GitHub 仓库链接。

[0.1.0]: https://github.com/Shadowell/Alpha/releases/tag/v0.1.0
