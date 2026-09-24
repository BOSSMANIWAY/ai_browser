"""Маленький contact-sheet (90px тайлы) + серия vision-тестов чтения подписей."""
import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.async_api import async_playwright
from agent.llm_client import PPLX_COOKIES, PPLX_ACCOUNT_ID, REQUEST_UUID, RUM_SESSION_ID

SHEET_JS = """async () => {
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
    const TILE = 90, LABEL = 20, COLS = 4;
    const rows = Math.ceil(cells.length / COLS);
    const canvas = document.createElement('canvas');
    canvas.width = COLS * TILE; canvas.height = rows * (TILE + LABEL);
    const ctx = canvas.getContext('2d');
    ctx.font = 'bold 14px monospace';
    const imgs = {};
    for (const [u, b64] of Object.entries(blobs)) {
        const im = new Image();
        await new Promise((res, rej) => { im.onload = res; im.onerror = rej;
                                         im.src = 'data:image/webp;base64,' + b64; });
        imgs[u] = im;
    }
    for (let i = 0; i < cells.length; i++) {
        const c = cells[i]; const im = imgs[c.img];
        const col = i % COLS, row = Math.floor(i / COLS);
        const x0 = col * TILE, y0 = row * (TILE + LABEL);
        if (im) {
            const sw = im.width, sh = im.height;
            const tw = sw / (c.sizeX/100), th = sh / (c.sizeY/100);
            const sx = Math.min(c.posX/100*(sw-tw), sw-tw), sy = Math.min(c.posY/100*(sh-th), sh-th);
            ctx.drawImage(im, sx, sy, tw, th, x0, y0, TILE, TILE);
        }
        ctx.fillStyle = '#000'; ctx.fillRect(x0, y0+TILE, TILE, LABEL);
        ctx.fillStyle = '#fff'; ctx.fillText(c.eid, x0+5, y0+TILE+15);
    }
    return {b64: canvas.toDataURL('image/jpeg', 0.7).split(',')[1], n: cells.length};
}"""


async def build():
    pw = await async_playwright().start()
    b = await pw.chromium.connect_over_cdp("http://127.0.0.1:9222", timeout=5000)
    pages = [p for ctx in b.contexts for p in ctx.pages]
    page = next((p for p in pages if "not-a-robot" in (p.url or "")), None)
    if not page:
        await pw.stop()
        return None
    r = await page.evaluate(SHEET_JS)
    await pw.stop()
    return r


def pplx_vision(msg, b64img):
    att = [{"file_view": {"kind": "image", "mime_type": "image/jpeg", "base64": b64img},
            "filename": "s.jpg"}]
    payload = {"params": {"attachments": att, "language": "ru-RU",
                          "frontend_uuid": REQUEST_UUID, "mode": "copilot",
                          "model_preference": "claude47opus", "prompt_source": "user",
                          "query_source": "home", "is_incognito": False,
                          "use_schematized_api": True,
                          "send_back_text_in_streaming_api": False,
                          "dsl_query": msg, "skip_search_enabled": True,
                          "version": "2.18", "rum_session_id": RUM_SESSION_ID},
               "query_str": msg}
    f = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w")
    json.dump(payload, f)
    pf = f.name
    f.close()
    cmd = ["curl", "--http2", "-s", "-N", "-X", "POST",
           "https://www.perplexity.ai/rest/sse/perplexity_ask",
           "--data-binary", "@" + pf,
           "-H", "accept: text/event-stream",
           "-H", "content-type: application/json",
           "-H", "origin: https://www.perplexity.ai",
           "-H", "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
           "-H", f"x-pplx-account: {PPLX_ACCOUNT_ID}",
           "-H", f"x-request-id: {uuid.uuid4()}",
           "-b", PPLX_COOKIES]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    out = proc.stdout.decode("utf-8", errors="ignore")
    os.unlink(pf)
    ans = re.findall(r'"answer":\s*"((?:[^"\\]|\\.)*)"', out)
    return ans[-1].encode().decode("unicode_escape", errors="ignore") if ans else "NO_ANSWER"


r = asyncio.run(build())
if not r or "error" in r:
    print("SHEET FAIL:", r)
    sys.exit(1)
print("small sheet:", r["n"], "tiles,", len(r["b64"]), "b64")
open("/tmp/small_sheet.jpg", "wb").write(base64.b64decode(r["b64"]))

for i in range(4):
    a = pplx_vision("Прочитай чёрные подписи под плитками первого ряда. [изображение прикреплено]", r["b64"])
    print(f"{i+1}:", a[:220])
    low = a.lower()
    if "не вижу" not in low and "нет доступа" not in low and "не могу" not in low:
        break
    time.sleep(3)
