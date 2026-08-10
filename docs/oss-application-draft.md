# Codex for OSS — Alpha application draft

Paste-ready answers for https://openai.com/zh-Hans-CN/form/codex-for-oss/  
Keep each free-text field ≤ 500 characters.

## Form fields

| Field | Value |
|---|---|
| GitHub username | `Shadowell` |
| GitHub repository URL | `https://github.com/Shadowell/Alpha` |
| Role | **Primary maintainer**（主要维护者） |

### Why does this repository qualify?（为什么这个代码仓库符合要求？）

```
Alpha is an MIT-licensed, actively maintained A-share quantitative screening system combining the Kronos K-line foundation model with a Hermes Agent self-evolution loop (funnel scoring, paper trading, FastAPI + Web UI). I’m the primary maintainer. Public traction: ~18 GitHub stars and 14 forks, with ongoing CI (pytest) and recent dependency/data-pipeline maintenance. It fills a niche most open quant stacks miss—practical Chinese-market research tooling that others can fork, audit, and extend.
```

(~430 chars)

### How will you use API credits for the project?（你将如何针对自己的项目使用 API 额度？）

```
Use Codex/API credits for maintainer workflows: PR review & refactoring, expanding offline pytest coverage for data/cache/Kronos paths, generating release notes/changelog drafts, dependency & CI hardening, and drafting security/docs improvements. Goal: reduce review latency and keep Alpha sustainable as forks/contributors grow—not for private trading signal generation.
```

(~360 chars)

### Interests

- Codex Security（建议勾选）
- 项目的 API 额度（建议勾选）

### Anything else?

```
Happy to share maintainer workflows publicly if useful to the Codex for OSS program. Repo docs now include bilingual README overview, CONTRIBUTING, SECURITY, and GitHub Issue/PR templates to make contribution and review smoother.
```

## Before submit checklist

- [ ] GitHub profile visibility = **Public**（Settings → Public profile / Appearances；确保个人页可被未登录访问）
- [ ] Repo visibility = **Public**（已是）
- [ ] Email on the form = ChatGPT account email
- [ ] This polish PR merged to `main` so reviewers see CONTRIBUTING / templates
