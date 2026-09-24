"""Одноразовый тест: снять скриншот страницы not-a-robot и сохранить
вместе с STATE_JS-списком элементов — чтобы увидеть, что «видит» модель."""
import asyncio
import base64
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright
from browser.browser_state import STATE_JS


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=5000)
    pages = []
    for ctx in browser.contexts:
        pages.extend(ctx.pages)
    page = next((p for p in pages if "not-a-robot" in (p.url or "")), None)
    if not page:
        print("NO_GAME_PAGE. pages:", [p.url[:60] for p in pages])
        return
    print("URL:", page.url)

    try:
        state = await asyncio.wait_for(page.evaluate(STATE_JS, 1), timeout=10)
    except Exception as e:
        print("STATE_FAIL:", e)
        return

    els = [e for e in state.get("elements", []) if e.get("img")]
    print(f"элементов с картинками: {len(els)}")
    for e in els[:25]:
        print(f"  [{e['eid']}] {e['tag']} img={e.get('img')} colors={e.get('colors')} selected={e.get('selected')} center=({e['x']},{e['y']})")

    shot = await page.screenshot(type="jpeg", quality=70)
    out = "/tmp/game_view.jpg"
    with open(out, "wb") as f:
        f.write(shot)
    print("screenshot saved:", out, len(shot), "bytes")
    await pw.stop()


asyncio.run(main())
