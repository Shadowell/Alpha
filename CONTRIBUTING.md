# Contributing to Alpha

Thanks for your interest in improving Alpha. This guide covers how to report issues, propose changes, and keep PRs reviewable.

中文说明见下方 [中文贡献指南](#中文贡献指南)。

## Ways to contribute

- Bug reports and reproducible failures
- Documentation / README / translation improvements
- Tests for uncovered API or data-path edge cases
- Features that fit the A-share screening / Kronos / Hermes Agent scope

Please open an issue first for large design changes so we can align before coding.

## Development setup

```bash
git clone https://github.com/Shadowell/Alpha.git
cd Alpha
python3 -m venv .venv
source .venv/bin/activate

# Minimal local runtime
pip install -r requirements-base.txt

# CI / offline test profile (recommended before opening a PR)
pip install -r requirements-ci.txt

cp .env.example .env
# Fill TUSHARE_TOKEN / LLM settings only if you need live data or agent features
```

Run the API locally:

```bash
./start.sh
# UI: http://127.0.0.1:18888
```

## Tests

```bash
# Offline API regression (preferred for PRs)
pytest tests/test_api_regression.py tests/test_dependency_profiles.py -q

# Broader suite when your change touches data / cache / Kronos paths
pytest -q
```

Do not commit secrets (`.env`, tokens, cookies). Prefer fixtures and mocks for network-dependent tests.

## Pull request checklist

1. Branch from `main`, keep the PR focused on one concern
2. Update docs if behavior or install steps change
3. Add / update tests when fixing bugs or changing APIs
4. Ensure CI-relevant tests pass locally
5. Fill the PR template with summary + test plan

## Code style

- Prefer clear names and small functions over clever abstractions
- Match existing patterns in `app/services/` and `app/routers/`
- Keep Chinese UI copy consistent with the current dashboard tone
- Avoid drive-by refactors unrelated to the PR goal

## Security

If you believe you found a vulnerability, see [SECURITY.md](SECURITY.md). Do not open a public issue for sensitive reports.

## License

By contributing, you agree that your contributions are licensed under the MIT License (see [LICENSE](LICENSE)).

---

## 中文贡献指南

欢迎通过 Issue / PR 参与 Alpha。

### 贡献类型

- Bug 复现与修复
- 文档、README、翻译
- 测试补强
- 与 A 股选股 / Kronos / Hermes Agent 相关的功能增强

较大设计变更请先开 Issue 讨论。

### 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-base.txt
# 提 PR 前建议再装：
pip install -r requirements-ci.txt
cp .env.example .env
./start.sh
```

### 提 PR 前请确认

1. 基于最新 `main`
2. 改动范围聚焦
3. 行为变更同步文档
4. 相关测试通过
5. 不要提交 `.env` / Token 等密钥

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。
