"""截取每个 Tab 的全页面截图。

使用 Playwright 移除所有 overflow/max-height 限制，
让页面内容完全展开后截取 full_page 截图。
"""
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18888"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

TABS = [
    ("market", "01-market.png"),
    ("data", "02-data.png"),
    ("funnel", "03-funnel.png"),
    ("notice", "04-notice.png"),
    ("agent", "05-agent.png"),
    ("paper", "06-paper.png"),
]

JS_EXPAND = """
(tabName) => {
    // 移除所有布局层级的滚动限制
    const selectors = [
        '.center-main', '.app-shell', '.layout',
        '.market-split-left', '.pool-list',
        '.dc-task-list', '.dc-report-body',
        '.sync-logs', '.notice-pool', '.dc-logs-section',
        '#agentSubMonitor', '#agentSubProposal',
        '.paper-list', '.right-panel',
        '.collapsible.expanded'
    ];
    selectors.forEach(sel => {
        document.querySelectorAll(sel).forEach(el => {
            el.style.maxHeight = 'none';
            el.style.height = 'auto';
            el.style.overflow = 'visible';
        });
    });

    // Agent 页面：只保留最新 2 条消息卡片
    if (tabName === 'agent') {
        const feed = document.getElementById('monitorFeed');
        if (feed) {
            const cards = feed.querySelectorAll('.monitor-msg-card');
            cards.forEach((card, i) => {
                if (i >= 2) card.style.display = 'none';
            });
            feed.style.maxHeight = 'none';
            feed.style.overflow = 'visible';
        }
    }

    // 全局去掉滚动限制
    document.body.style.overflow = 'visible';
    document.body.style.height = 'auto';
    document.documentElement.style.overflow = 'visible';
    document.documentElement.style.height = 'auto';
    // 隐藏非当前 tab 内容
    document.querySelectorAll('.tab-content:not(.active)').forEach(el => {
        el.style.display = 'none';
    });
    // 隐藏右侧面板（如果没数据的话只是空白）
    const rp = document.querySelector('.right-panel');
    if (rp && !rp.classList.contains('visible')) {
        rp.style.display = 'none';
    }
}
"""

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})

        for tab, fname in TABS:
            url = f"{BASE}/?tab={tab}"
            print(f"[{tab}] navigating...")
            page.goto(url, wait_until="networkidle")
            time.sleep(4)

            page.evaluate(JS_EXPAND, tab)
            time.sleep(1)

            dest = OUT / fname
            page.screenshot(path=str(dest), full_page=True)
            size = dest.stat().st_size // 1024
            print(f"[{tab}] saved → {dest} ({size}KB)")

        browser.close()
    print("done")

if __name__ == "__main__":
    main()
