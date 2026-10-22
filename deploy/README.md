# Alpha 自动部署说明

推送 `main` 后由 GitHub Actions 自托管 Runner 在 tokyo 服务器（64.83.43.61）本机完成部署，
不经过 GitHub 托管机 SSH。

## 运行位置

- Runner 目录：`/opt/actions-runner/Alpha`
- Runner 名称：`alpha-prod-64-83-43-61`，标签 `alpha-production`
- systemd 服务：`actions.runner.Shadowell-Alpha.alpha-prod-64-83-43-61.service`
- 部署目录：`/opt/alpha`（`.env`、`data/`、`logs/`、`.git/` 与 `deploy/last_deployed_sha` 不参与同步覆盖）
- 容器：`alpha`，镜像 `alpha:latest`，端口仅绑定 `127.0.0.1:18888`
- 对外入口：`https://alpha.notenap.com`（Nginx SNI 分流 → `127.0.0.1:8461`，启用 Basic Auth）

## 重新注册 Runner

服务器上的 Runner 以 root 运行，`config.sh` 需要显式允许：

```bash
TOKEN=$(gh api -X POST repos/Shadowell/Alpha/actions/runners/registration-token --jq .token)
cd /opt/actions-runner/Alpha
RUNNER_ALLOW_RUNASROOT=1 ./config.sh \
  --url https://github.com/Shadowell/Alpha \
  --token "$TOKEN" \
  --name alpha-prod-64-83-43-61 \
  --labels alpha-production \
  --unattended --replace
./svc.sh install && ./svc.sh start
```

Runner 版本需与机队其他 Runner 一致（当前 2.337.0），下载源：
`https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz`

## 手动触发与跳过判定

- `workflow_dispatch` 勾选 `force_deploy` 可强制重新部署同一 SHA。
- 常规 push 只在 `GITHUB_SHA` 与 `/opt/alpha/deploy/last_deployed_sha` 不同时执行。

## 冒烟检查

```bash
curl -fsS http://127.0.0.1:18888/ >/dev/null                       # 容器健康
curl -fsS http://127.0.0.1:18888/api/jobs/kline-cache/enrichment-status >/dev/null
# WebSocket 必须用 HTTP/1.1 验证，HTTP/2 下 Upgrade 头无效会返回 404
curl -sS -i --http1.1 -u "$USER:$PASS" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  https://alpha.notenap.com/ws/realtime | head -1
```

## 回滚

```bash
cd /opt/alpha && docker compose down            # 保留 alpha_data 命名卷
git -C /opt/alpha revert <sha>                  # 代码回滚后 push main 触发重新部署
```
