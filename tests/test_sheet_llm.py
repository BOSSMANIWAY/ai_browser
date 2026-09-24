"""One-off тест: contact-sheet → Perplexity vision. Ожидаем e6,e7,e10,e11."""
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright
from agent.llm_client import LLMClient

SHEET_JS = open(os.path.join(os.path.dirname(__file__), "_sheet_js.txt")).read() if os.path.exists(os.path.join(os.path.dirname(__file__), "_sheet_js.txt")) else None


async def build_sheet(page):
    js = """async () => {
        const cells = [];
        for (const el of document.querySelectorAll('[data-eid]')) {
            const st = el.getAttribute('style') || '';
            const m = st.match(/background-image:\\s*url\\("?([^")]+)"?\\)/);
            if (!m) continue;
            const p = st.match(/background-position:\\s*([\\d.]+)%\\s+([\\d.]+)%/);
            const s = st.match(/background-size:\\s*([\\d.]+)%\\s+([\\d.]+)%/);
            cells.push({eid: el.getAttribute('data-eid'), img: m[1],
                        posX: p ? +p[1] : 0, posY: p ? +p[2] : 0,
                        sizeX: s ? +s[1] : 400, sizeY: s ? +s[2] : 400});
        }
        if (!cells.length) return {error: 'no cells'};
        const urls = [...new Set(cells.map(c => c.img))];
        const blobs = {};
        for (const u of urls) {
            const resp = await fetch(new URL(u, location.href).href);
            const buf = await resp.arrayBuffer();
            let bin = '';
            const bytes = new Uint8Array(buf);
            for (let i = 0; i < bytes.length; i += 0x8000)
                bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
            blobs[u] = btoa(bin);
        }
        const TILE = 150, LABEL = 26, COLS = 4;
        const rows = Math.ceil(cells.length / COLS);
        const canvas = document.createElement('canvas');
        canvas.width = COLS * TILE;
        canvas.height = rows * (TILE + LABEL);
        const ctx = canvas.getContext('2d');
        ctx.font = 'bold 20px monospace';
        const imgs = {};
        for (const [u, b64] of Object.entries(blobs)) {
            const im = new Image();
            await new Promise((res, rej) => { im.onload = res; im.onerror = rej;
                                             im.src = 'data:image/webp;base64,' + b64; });
            imgs[u] = im;
        }
        for (let i = 0; i < cells.length; i++) {
            const c = cells[i];
            const im = imgs[c.img];
            const col = i % COLS, row = Math.floor(i / COLS);
            const x0 = col * TILE, y0 = row * (TILE + LABEL);
            if (im) {
                const sw = im.width, sh = im.height;
                const tw = sw / (c.sizeX / 100), th = sh / (c.sizeY / 100);
                const sx = Math.min(c.posX / 100 * (sw - tw), sw - tw);
                const sy = Math.min(c.posY / 100 * (sh - th), sh - th);
                ctx.drawImage(im, sx, sy, tw, th, x0, y0, TILE, TILE);
            }
            ctx.fillStyle = '#000';
            ctx.fillRect(x0, y0 + TILE, TILE, LABEL);
            ctx.fillStyle = '#fff';
            ctx.fillText(c.eid, x0 + 8, y0 + TILE + 20);
        }
        return {b64: canvas.toDataURL('image/jpeg', 0.8).split(',')[1], n: cells.length};
    }"""
    return await asyncio.wait_for(page.evaluate(js), timeout=20)


async def main():
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=5000)
    pages = [p for ctx in browser.contexts for p in ctx.pages]
    page = next((p for p in pages if "not-a-robot" in (p.url or "")), None)
    if not page:
        print("NO_GAME_PAGE")
        return
    result = await build_sheet(page)
    if "error" in result:
        print("ERR:", result["error"])
        return
    print(f"sheet: {result['n']} tiles, {len(result['b64'])} b64")
    await pw.stop()

    llm = LLMClient(provider="pplx", model="claude47opus")
    msg = (
        "Это contact-sheet игровой сетки 4x4: 16 плиток, под каждой чёрная "
        "подпись с eID (e4..e19). Задание уровня: выбери все клетки со знаком "
        "STOP (красный восьмиугольник). ВНИМАНИЕ: знак может быть частично "
        "виден в плитке (край знака, угол) — такая клетка тоже считается. "
        "Ответь СТРОГО JSON: {\"stops\": [\"eN\", ...], \"others\": [кратко что в остальных]}"
    )
    for attempt in range(3):
        ans = await llm.chat_with_image(msg, result["b64"], "sheet.jpg", "image/jpeg")
        print(f"--- попытка {attempt+1}: {ans[:300]}")
        if "не вижу" not in ans.lower() and len(ans) > 10:
            break
        await asyncio.sleep(3)


asyncio.run(main())