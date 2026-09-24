"""Временный скрипт: mini-sheet по одному ряду (4 клетки) как маленькие PNG
для проверки доставки в pplx. Затем будет встроен в агента."""
import asyncio, base64, sys, json
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.async_api import async_playwright
from browser.browser_state import STATE_JS

# JS: собираем клетки по рядам и рисуем каждый ряд отдельным canvas (подписи eID)
ROW_SHEETS_JS = """() => {
    const cells = [];
    for (const el of document.querySelectorAll('[data-eid]')) {
        const st = el.getAttribute('style') || '';
        if (!st.includes('background-image')) continue;
        const m = st.match(/background-position:\\s*([\\d.]+)%\\s+([\\d.]+)%/);
        const mi = st.match(/background-image:\\s*url\\(["']?([^"')]+)["']?\\)/);
        const ms = st.match(/background-size:\\s*([\\d.]+)%\\s+([\\d.]+)%/);
        if (!m || !mi) continue;
        cells.push({id: el.getAttribute('data-eid'), x: parseFloat(m[1]), y: parseFloat(m[2]),
                    img: mi[1], sx: ms ? parseFloat(ms[1]) : 400, sy: ms ? parseFloat(ms[2]) : 400});
    }
    if (!cells.length) return {error: 'no cells'};
    const byRow = {};
    for (const c of cells) {
        const key = c.y + '|' + c.img;
        (byRow[key] = byRow[key] || []).push(c);
    }
    const rows = Object.values(byRow).map(r => r.sort((a,b)=>a.x-b.x));
    rows.sort((r1,r2)=>r1[0].y-r2[0].y);
    const TILE = 96, LBL = 26;
    return rows.map((row, ri) => {
        const c = document.createElement('canvas');
        c.width = TILE*row.length; c.height = TILE+LBL;
        const ctx = c.getContext('2d');
        ctx.fillStyle = '#000'; ctx.fillRect(0,0,c.width,c.height);
        row.forEach((cell, i) => {
            const im = new Image();
            im.src = cell.img;
            // синхронно нельзя; используем уже загруженные браузером картинки через decode
        });
        // Обход: кроп через fetch+blob+Image уже загруженного в DOM background.
        // Проще: рисуем фон клетки напрямую: временный div с тем же style → background
        // рендерится браузером, но в canvas его не перенести без Image.
        return {ids: row.map(x=>x.id), needAsync: true};
    });
}"""

# Правильная асинхронная версия: сначала грузим все спрайты, потом рисуем
ROW_SHEETS_JS_ASYNC = """async () => {
    const cells = [];
    for (const el of document.querySelectorAll('[data-eid]')) {
        const st = el.getAttribute('style') || '';
        if (!st.includes('background-image')) continue;
        const m = st.match(/background-position:\\s*([\\d.]+)%\\s+([\\d.]+)%/);
        const mi = st.match(/background-image:\\s*url\\(["']?([^"')]+)["']?\\)/);
        const ms = st.match(/background-size:\\s*([\\d.]+)%\\s+([\\d.]+)%/);
        if (!m || !mi) continue;
        cells.push({id: el.getAttribute('data-eid'), x: parseFloat(m[1]), y: parseFloat(m[2]),
                    img: mi[1], sx: ms ? parseFloat(ms[1]) : 400, sy: ms ? parseFloat(ms[2]) : 400});
    }
    if (!cells.length) return {error: 'no cells'};
    // resolve url (может быть относительным)
    cells.forEach(c => { c.img = new URL(c.img, location.href).href; });
    // уникальные спрайты
    const uniq = [...new Set(cells.map(c=>c.img))];
    const imgs = {};
    for (const u of uniq) {
        const im = new Image();
        im.src = u;
        await im.decode().catch(()=>{});
        imgs[u] = im;
    }
    const byRow = {};
    for (const c of cells) {
        const key = c.y + '|' + c.img;
        (byRow[key] = byRow[key] || []).push(c);
    }
    let rows = Object.values(byRow).map(r => r.sort((a,b)=>a.x-b.x));
    rows.sort((r1,r2)=>r1[0].y-r2[0].y);
    const TILE = 96, LBL = 26;
    const out = [];
    for (const row of rows) {
        const n = row.length;
        const cv = document.createElement('canvas');
        cv.width = TILE*n; cv.height = TILE+LBL;
        const ctx = cv.getContext('2d');
        ctx.fillStyle = '#000'; ctx.fillRect(0,0,cv.width,cv.height);
        row.forEach((cell, i) => {
            const im = imgs[cell.img];
            const cols = Math.round(cell.sx/100*4) || 4;   // 400% => 4x4 сетка
            const rowsN = Math.round(cell.sy/100*4) || 4;
            const tw = im.naturalWidth/cols, th = im.naturalHeight/rowsN;
            const fx = cell.x/100*(im.naturalWidth-tw);
            const fy = cell.y/100*(im.naturalHeight-th);
            try { ctx.drawImage(im, fx, fy, tw, th, i*TILE, 0, TILE, TILE); } catch(e) {}
            ctx.fillStyle = '#000'; ctx.fillRect(i*TILE, TILE, TILE, LBL);
            ctx.fillStyle = '#fff'; ctx.font = 'bold 15px monospace'; ctx.textAlign='center';
            ctx.fillText(cell.id, i*TILE+TILE/2, TILE+18);
        });
        out.push({ids: row.map(x=>x.id), b64: cv.toDataURL('image/png').split(',')[1]});
    }
    return {rows: out};
}"""


async def main():
    pw = await async_playwright().start()
    b = await pw.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=5000)
    pages = [p for ctx in b.contexts for p in ctx.pages]
    page = next((p for p in pages if 'not-a-robot' in (p.url or '')), None)
    if not page:
        print('NO GAME TAB')
        return
    await asyncio.wait_for(page.evaluate(STATE_JS, 1), timeout=10)
    r = await page.evaluate(ROW_SHEETS_JS_ASYNC)
    if 'error' in r:
        print('ERR:', r['error'])
        return
    for i, row in enumerate(r['rows']):
        fn = f'/tmp/rowsheet_{i}.png'
        open(fn, 'wb').write(base64.b64decode(row['b64']))
        print(f'row{i} {row["ids"]} -> {fn} {len(row["b64"])//1024}KB b64')
    await pw.stop()


if __name__ == '__main__':
    asyncio.run(main())
