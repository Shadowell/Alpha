"""截取每个 Tab 的分段截图。

先用 Playwright 截取 full_page 截图，再用 Pillow 裁剪为多段，
确保在 GitHub README 中每张图都能清晰展示。
"""
import time
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:18888"
OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

TABS = [
    ("market", "01-market"),
    ("data", "02-data"),
    ("funnel", "03-funnel"),
    ("notice", "04-notice"),
    ("agent", "05-agent"),
    ("paper", "06-paper"),
]

WIDTH = 1280
SPLIT_THRESHOLD = 1100
SEGMENT_HEIGHT = 900
OVERLAP = 80

JS_EXPAND = """
(tabName) => {
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

    // 大盘：热门个股只显示前 10
    if (tabName === 'market') {
        const hotStocks = document.getElementById('hotStocks');
        if (hotStocks) {
            const items = hotStocks.querySelectorAll('.hot-stock-item');
            items.forEach((item, i) => { if (i >= 10) item.style.display = 'none'; });
        }
    }

    // Agent：只保留最新 2 条消息
    if (tabName === 'agent') {
        const feed = document.getElementById('monitorFeed');
        if (feed) {
            const cards = feed.querySelectorAll('.monitor-msg-card');
            cards.forEach((card, i) => { if (i >= 2) card.style.display = 'none'; });
            feed.style.maxHeight = 'none';
            feed.style.overflow = 'visible';
        }
    }

    document.body.style.overflow = 'visible';
    document.body.style.height = 'auto';
    document.documentElement.style.overflow = 'visible';
    document.documentElement.style.height = 'auto';
    document.querySelectorAll('.tab-content:not(.active)').forEach(el => {
        el.style.display = 'none';
    });
    const rp = document.querySelector('.right-panel');
    if (rp && !rp.classList.contains('visible')) {
        rp.style.display = 'none';
    }
}
"""


def split_image(img_path: Path, prefix: str) -> list[str]:
    """将截图裁剪为多段。短图保留原样，长图裁剪为多段。"""
    im = Image.open(img_path)
    w, h = im.size
    print(f"  full image: {w}x{h}")

    if h <= SPLIT_THRESHOLD:
        dest = OUT / f"{prefix}.png"
        im.save(dest, optimize=True)
        print(f"  → {dest.name} ({dest.stat().st_size // 1024}KB)")
        return [dest.name]

    files = []
    part = 1
    y = 0
    while y < h:
        bottom = min(y + SEGMENT_HEIGHT, h)
        if h - bottom < 200 and bottom < h:
            bottom = h
        seg = im.crop((0, y, w, bottom))
        dest = OUT / f"{prefix}-{part}.png"
        seg.save(dest, optimize=True)
        print(f"  → {dest.name} ({dest.stat().st_size // 1024}KB) [{w}x{bottom - y}]")
        files.append(dest.name)
        y = bottom - OVERLAP
        part += 1
        if bottom == h:
            break

    return files


def main():
    all_files = {}
    tmp_dir = OUT / "_tmp"
    tmp_dir.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": 900})

        for tab, prefix in TABS:
            url = f"{BASE}/?tab={tab}"
            print(f"\n[{tab}] navigating...")
            page.goto(url, wait_until="networkidle")
            time.sleep(4)
            page.evaluate(JS_EXPAND, tab)
            time.sleep(1)

            tmp_path = tmp_dir / f"{prefix}_full.png"
            page.screenshot(path=str(tmp_path), full_page=True)
            print(f"  full screenshot: {tmp_path.stat().st_size // 1024}KB")

            files = split_image(tmp_path, prefix)
            all_files[tab] = files
            tmp_path.unlink()

        browser.close()

    tmp_dir.rmdir()

    print("\n=== Summary ===")
    for tab, files in all_files.items():
        print(f"  {tab}: {files}")
    print("done")


if __name__ == "__main__":
    main()
