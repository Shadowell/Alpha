#!/usr/bin/env bash
# 一键跑 Alpha 全站自动化测试：API 回归 + Playwright E2E
# 前提：后端服务已启动（./start.sh 或 ./restart.sh）
set -euo pipefail

BASE_URL="${ALPHA_TEST_BASE_URL:-http://127.0.0.1:18890}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPORT_FILE="${SCRIPT_DIR}/test_report.txt"

cd "${ROOT_DIR}"

echo "================================================================"
echo " Alpha 自动化测试 - 启动"
echo " 后端: ${BASE_URL}"
echo " 时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"

# 1. 检查后端存活
if ! curl -s -m 5 "${BASE_URL}/api/agent/status" > /dev/null; then
  echo "[ERROR] 后端未启动，请先运行 ./start.sh"
  exit 1
fi
echo "[OK] 后端已在线"

: > "${REPORT_FILE}"
API_STATUS=0
E2E_STATUS=0

# 2. API 回归
echo ""
echo "───── 1. API 回归测试 ─────"
if python3 -m pytest tests/test_api_regression.py -v --tb=short --no-header 2>&1 | tee -a "${REPORT_FILE}" | tail -5; then
  echo "[OK] API 测试通过"
else
  API_STATUS=$?
  echo "[FAIL] API 测试失败"
fi

# 3. E2E
echo ""
echo "───── 2. Playwright E2E 测试 ─────"
cd "${SCRIPT_DIR}/e2e"
if [ ! -d "node_modules" ]; then
  echo "[INFO] 首次运行，安装依赖..."
  npm install --no-audit --no-fund --prefer-offline
fi
if npx playwright test --config=playwright.config.js 2>&1 | tee -a "${REPORT_FILE}" | tail -8; then
  echo "[OK] E2E 测试通过"
else
  E2E_STATUS=$?
  echo "[FAIL] E2E 测试失败"
fi

# 4. 汇总
cd "${ROOT_DIR}"
echo ""
echo "================================================================"
if [ $API_STATUS -eq 0 ] && [ $E2E_STATUS -eq 0 ]; then
  echo " ✓ 全部通过"
  exit 0
else
  echo " ✗ 有失败用例 (API=${API_STATUS}, E2E=${E2E_STATUS})"
  echo " 详细报告: ${REPORT_FILE}"
  exit 1
fi
