"""
Browser State v2 - Снимок состояния страницы со стабильными element_id

Каждый интерактивный элемент получает стабильный id (e1, e2, ...).
LLM ссылается на элементы по id: click(e17), type(e5, "text").
Селекторы больше не нужны — агент не зависит от хрупких CSS/XPath.
"""

import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# JS: размечает интерактивные элементы data-eid и собирает компактный снимок.
# Параметр startCounter — продолжение нумерации eid для дочерних фреймов.
# ВАЖНО: сырая строка (r"""...""") — внутри JS-регулярки с \d, \s и т.п.
STATE_JS = r"""
(startCounter) => {
    // 1. Снимаем старую разметку
    document.querySelectorAll('[data-eid]').forEach(el => el.removeAttribute('data-eid'));

    // 2. Находим интерактивные элементы — ВКЛЮЧАЯ shadow DOM и same-origin iframe
    const selector = [
        'a[href]',
        'button',
        'input:not([type="hidden"])',
        'textarea',
        'select',
        '[role="button"]',
        '[role="link"]',
        '[role="tab"]',
        '[role="checkbox"]',
        '[role="radio"]',
        '[role="combobox"]',
        '[contenteditable="true"]',
        '[onclick]',
        '[tabindex]:not([tabindex="-1"])',
        'canvas',
        'svg [fill]:not([fill="none"]), svg [stroke]:not([stroke="none"])'
    ].join(', ');

    // Игровые страницы часто используют div-области с pointer-событиями
    // вместо семантических button/input. Берём только видимые кликабельные области.
    const pointerCandidates = [];
    try {
        for (const el of document.querySelectorAll('div, section, span, li')) {
            const style = window.getComputedStyle(el);
            if (style.cursor === 'pointer' || style.touchAction === 'none') {
                pointerCandidates.push(el);
            }
        }
    } catch (e) {}

    // Рекурсивный обход: document -> shadow roots
    // (кросс-origin iframe обрабатываются снаружи через Playwright frames)
    function deepQueryAll(sel, root, out) {
        try {
            out.push(...root.querySelectorAll(sel));
        } catch (e) {}
        // Shadow roots
        let children = [];
        try { children = root.querySelectorAll('*'); } catch (e) {}
        for (const el of children) {
            if (el.shadowRoot) deepQueryAll(sel, el.shadowRoot, out);
        }
        return out;
    }

    const candidates = deepQueryAll(selector, document, []);
    for (const el of pointerCandidates) {
        if (!candidates.includes(el)) candidates.push(el);
    }
    const interactive = [];
    let counter = startCounter;

    for (const el of candidates) {
        // Видимость: в DOM, имеет размеры, не display:none
        const rect = el.getBoundingClientRect();
        const style = window.getComputedStyle(el);
        const visible = rect.width > 0 && rect.height > 0
            && style.display !== 'none'
            && style.visibility !== 'hidden'
            && style.opacity !== '0';
        if (!visible) continue;

        // В viewport-окрестности (включая чуть за пределами для scroll)
        if (rect.bottom < -200 || rect.top > window.innerHeight + 200) continue;

        const eid = 'e' + (counter++);
        el.setAttribute('data-eid', eid);

        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || '';
        let text = '';

        if (tag === 'input') {
            if (type === 'submit' || type === 'button') {
                text = el.value || '';
            } else {
                text = el.placeholder || el.getAttribute('aria-label') || el.name || '';
            }
        } else {
            text = (el.innerText || el.textContent || el.value || '').trim().substring(0, 120);
        }

        // Родительская форма (для контекста)
        const form = el.closest('form');
        const formId = form ? (form.id || form.getAttribute('name') || 'form') : null;

        // Картинка внутри клетки (игровые сетки): src + цветовой профиль.
        // Имя файла часто семантично (stop-sign-3.jpg), цвет — для визуальных CAPTCHA.
        function imgProfile(node) {
            let img = node.tagName === 'IMG' ? node : node.querySelector('img');
            let src = img ? img.src : '';
            if (!src) {
                const bg = getComputedStyle(node).backgroundImage || '';
                let m = bg.match(/url\("?([^")]+)"?\)/);
                if (!m && node.parentElement) {
                    const pbg = getComputedStyle(node.parentElement).backgroundImage || '';
                    m = pbg.match(/url\("?([^")]+)"?\)/);
                }
                if (m) src = m[1];
            }
            if (!src) return null;
            const base = src.split('/').pop().split('?')[0].substring(0, 60);
            const info = { img: base };
            try {
                if (img && img.complete && img.naturalWidth > 0) {
                    const c = document.createElement('canvas');
                    c.width = 24; c.height = 24;
                    const ctx = c.getContext('2d');
                    ctx.drawImage(img, 0, 0, 24, 24);
                    const d = ctx.getImageData(0, 0, 24, 24).data;
                    let red = 0, green = 0, blue = 0, yellow = 0, dark = 0, total = 0;
                    for (let i = 0; i < d.length; i += 4) {
                        const r = d[i], g = d[i+1], b = d[i+2];
                        total++;
                        if (r > 120 && r > g * 1.4 && r > b * 1.4) red++;
                        if (g > 100 && g > r * 1.25 && g > b * 1.25) green++;
                        if (b > 110 && b > r * 1.3 && b > g * 1.1) blue++;
                        if (r > 150 && g > 120 && b < 100) yellow++;
                        if (r < 60 && g < 60 && b < 60) dark++;
                    }
                    const pct = (n) => Math.round(n / total * 100);
                    info.colors = { red: pct(red), green: pct(green), blue: pct(blue), yellow: pct(yellow), dark: pct(dark) };
                }
            } catch (e) { /* CORS canvas taint — цвет недоступен */ }
            return info;
        }
        const ip = imgProfile(el);

        // Состояние выбора (игровые клетки): aria + классы
        const clsAll = ((el.className + ' ' + (el.parentElement ? el.parentElement.className : '')) + '').toLowerCase();
        const selected = el.getAttribute('aria-pressed') === 'true'
            || el.getAttribute('aria-checked') === 'true'
            || /(^|[\s_-])(selected|checked|chosen|active)([\s_-]|$)/.test(clsAll);

        // Внутри модального окна? (помогает отличить submit-кнопку формы
        // от похожих кнопок-опций на фоне)
        const inDialog = !!el.closest('dialog[open], [role="dialog"], [role="alertdialog"], [class*="modal" i], [class*="Modal"], [class*="popup" i]');

        interactive.push({
            eid: eid,
            tag: tag,
            type: type || undefined,
            text: text || undefined,
            x: Math.round(rect.left + rect.width / 2),
            y: Math.round(rect.top + rect.height / 2),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
            cursor: style.cursor || undefined,
            img: ip ? ip.img : undefined,
            colors: ip && ip.colors ? ip.colors : undefined,
            selected: selected || undefined,
            href: tag === 'a' ? el.href.substring(0, 200) : undefined,
            name: el.name || undefined,
            id_attr: el.id || undefined,
            placeholder: el.placeholder || undefined,
            value: ((tag === 'input' || tag === 'textarea') && el.value && type !== 'password') ? el.value.substring(0, 4000) : undefined,
            required: el.required || undefined,
            disabled: el.disabled || undefined,
            checked: el.checked || undefined,
            form: formId || undefined,
            aria: el.getAttribute('aria-label') || undefined,
            role: el.getAttribute('role') || undefined,
            in_dialog: inDialog || undefined,
            in_viewport: rect.top >= 0 && rect.bottom <= window.innerHeight
        });
    }

    return {
        url: window.location.href,
        title: document.title,
        readyState: document.readyState,
        scrollY: Math.round(window.scrollY),
        pageHeight: document.documentElement.scrollHeight,
        viewportHeight: window.innerHeight,
        elements: interactive,
        // Структура страницы: заголовки — помогают понять тип и назначение страницы
        headings: [...document.querySelectorAll('h1, h2, h3')]
            .map(h => (h.innerText || '').trim().substring(0, 100))
            .filter(t => t.length > 0)
            .slice(0, 10),
        // Компактный текст страницы для контекста
        page_text: document.body ? document.body.innerText.substring(0, 3000) : '',
        // Детект типовых препятствий
        game: (() => {
            const body = document.body ? document.body.innerText : '';
            const level = body.match(/Level\s+(\d+)\s*:\s*([^\n]+)/i);
            const canvases = [...document.querySelectorAll('canvas')].map((c, i) => {
                const r = c.getBoundingClientRect();
                return {index: i, x: Math.round(r.left), y: Math.round(r.top), width: Math.round(r.width), height: Math.round(r.height)};
            }).filter(c => c.width > 0 && c.height > 0);
            return {
                detected: /neal\.fun\/not-a-robot|I'm Not a Robot/i.test(location.href + ' ' + document.title + ' ' + body),
                level: level ? Number(level[1]) : null,
                name: level ? level[2].trim().substring(0, 120) : null,
                canvases,
                interactive_count: document.querySelectorAll('button, input, [role="button"], canvas, [onclick], [tabindex]:not([tabindex="-1"])').length
            };
        })(),
        flags: {
            captcha: !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, .h-captcha'),
            cookie_banner: !!Array.from(document.querySelectorAll('div, section, dialog')).find(el => {
                const id = (el.id + ' ' + el.className).toLowerCase();
                return /cookie|consent|gdpr/.test(id) && el.getBoundingClientRect().height > 30;
            }),
            login_form: !!document.querySelector('input[type="password"]')
        },
        // Подписи и подсказки рядом с полями: ВСЕ тексты рядом с input,
        // БЕЗ фильтра по ключевым словам — семантику оценивает LLM
        // по тексту и цвету (как человек глазами)
        field_context: (() => {
            const out = [];
            const seen = new Set();
            for (const input of document.querySelectorAll('input, textarea, select')) {
                const r = input.getBoundingClientRect();
                if (r.width < 30) continue;
                const eid = input.getAttribute('data-eid') || '?';
                // Маска/placeholder/текущее значение: человек видит "+7 (___) ___"
                // прямо в поле — даём модели то же самое
                const mask = input.placeholder || input.getAttribute('data-mask') || '';
                const curValue = (input.value || '').substring(0, 40);
                const label = input.closest('label');
                const labelText = label ? (label.innerText || '').trim().substring(0, 80) : '';
                const info = { eid };
                if (mask) info.mask = mask;
                if (curValue) info.current_value = curValue;
                if (labelText) info.label = labelText;
                if (mask || curValue || labelText) out.push(info);
                for (const sib of (input.closest('label') || input.closest('div, fieldset, p') || input.parentElement || document.body).querySelectorAll('label, div, span, p, small, strong, em')) {
                    if (sib.contains(input) || sib.querySelector('input, textarea, select')) continue;
                    const sr = sib.getBoundingClientRect();
                    if (sr.width < 30 || sr.height < 8) continue;
                    // Рядом с полем (ниже или выше на 60px)
                    if (Math.abs(sr.top - r.bottom) > 60 && Math.abs(r.top - sr.bottom) > 60) continue;
                    const text = (sib.innerText || '').trim();
                    if (text.length < 2 || text.length > 200 || seen.has(text)) continue;
                    seen.add(text);
                    out.push({ eid, text, color: window.getComputedStyle(sib).color });
                }
            }
            return out.slice(0, 12);
        })(),
        // Открытые модалки/dialog — для state_diff и для решения LLM.
        // Кнопки внутри диалога критичны: без них модель видит только текст
        // и пытается закрыть окно крестиком, попадая в цикл «открылось→закрылось».
        dialog: (() => {
            const dialogs = [...document.querySelectorAll(
                'dialog[open], [role="dialog"], [role="alertdialog"], [class*="modal" i], [class*="Modal"], [class*="popup" i]'
            )].filter(d => {
                const r = d.getBoundingClientRect();
                const s = window.getComputedStyle(d);
                return r.width > 100 && r.height > 50 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            });
            // Кнопка, которая ПРОДОЛЖАЕТ работу (а не отменяет её).
            // ВАЖНО: \b в JS не работает с кириллицей (ASCII word boundary),
            // поэтому для «ок» границы задаём явно через \s и якоря.
            const proceedRe = /сохран|продолж|подтвер|примен|отправ|далее|дальше|готово|заверш|выбрать|выбрана|принять|agree|accept|continue|save|confirm|submit|next|apply|done|select|(?:^|\s)ok(?:\s|$)|(?:^|\s)okay(?:\s|$)|(?:^|\s)ок(?:\s|$)/i;
            const cancelRe = /закрыть|отмен|сброс|не сейчас|позже|крест|close|cancel|dismiss|skip|later|×|✕|✖/i;
            const buttons = [];
            let hasProceed = false, hasCancel = false, hasRequired = false, hasInput = false;
            for (const d of dialogs) {
                for (const b of d.querySelectorAll('[data-eid]')) {
                    const tag = b.tagName.toLowerCase();
                    const txt = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().substring(0, 60);
                    const isBtn = tag === 'button' || b.getAttribute('role') === 'button'
                        || tag === 'input' && ['submit', 'button'].includes((b.getAttribute('type') || '').toLowerCase());
                    if (isBtn) {
                        if (proceedRe.test(txt)) hasProceed = true;
                        if (cancelRe.test(txt)) hasCancel = true;
                        if (txt || b.getAttribute('aria-label')) {
                            buttons.push({ eid: b.getAttribute('data-eid'), text: txt || b.getAttribute('aria-label') });
                        }
                    }
                    const isField = tag === 'input' || tag === 'select' || tag === 'textarea'
                        || b.getAttribute('role') === 'combobox'
                        || b.getAttribute('contenteditable') === 'true';
                    if (isField && (b.getAttribute('type') || '') !== 'hidden') hasInput = true;
                    if (isField && b.required) hasRequired = true;
                }
            }
            const titleEl = dialogs.length ? dialogs[0].querySelector('h1, h2, h3, [role="heading"]') : null;
            return {
                open: dialogs.length > 0,
                count: dialogs.length,
                text: dialogs.map(d => (d.innerText || '').substring(0, 300)).join(' | ').substring(0, 500),
                title: titleEl ? (titleEl.innerText || '').trim().substring(0, 120) : '',
                buttons: buttons.slice(0, 12),
                // blocking: окно ТРЕБУЕТ действия, а не информирует.
                // Обязательное поле — точно блокирует. Просто поле + кнопка
                // подтверждения — тоже (форма данных). Промо-окно с одной
                // кнопкой «ОК» и без полей остаётся закрываемым.
                blocking: dialogs.length > 0 && (hasRequired || (hasInput && hasProceed)),
                has_proceed: hasProceed,
                has_cancel: hasCancel,
                has_input: hasInput,
                has_required_field: hasRequired
            };
        })(),
        // Заметные сообщения: элементы с ARIA-ролями ИЛИ выделенные цветом
        // (красный/оранжевый/зелёный текст) — БЕЗ фильтра по словам.
        // Человек замечает такое глазами; здесь даём модели то же самое:
        // текст + цвет, семантику (ошибка/успех/инфо) оценивает LLM.
        notices: (() => {
            const out = [];
            const seen = new Set();
            const push = (el) => {
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                if (r.width < 30 || r.height < 10 || s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return;
                const text = (el.innerText || '').trim();
                if (text.length < 3 || text.length > 300 || seen.has(text)) return;
                seen.add(text);
                out.push({ text, color: s.color, role: el.getAttribute('role') || undefined });
            };
            // 1. ARIA-роли уведомлений (стандартный способ)
            for (const el of document.querySelectorAll('[role="alert"], [role="status"], [aria-live="assertive"], [aria-live="polite"]')) {
                push(el);
            }
            // 2. Цветной текст: красные/оранжевые/зелёные оттенки = визуальное выделение
            for (const el of document.querySelectorAll('div, span, p, li, td, strong, b')) {
                if (el.closest('[role="alert"], [role="status"]')) continue;
                const s = window.getComputedStyle(el);
                const m = s.color.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
                if (!m) continue;
                const [rr, gg, bb] = [+m[1], +m[2], +m[3]];
                const isReddish = rr > 130 && rr > gg * 1.4 && rr > bb * 1.4;
                const isOrange = rr > 150 && gg > 80 && gg < rr * 0.85 && bb < 100;
                const isGreen = gg > 100 && gg > rr * 1.3 && gg > bb * 1.3;
                if (isReddish || isOrange || isGreen) push(el);
            }
            return out.slice(0, 8);
        })()
    };
}
"""


class BrowserState:
    """Снимок состояния страницы для LLM"""

    def __init__(self, data: Dict[str, Any]):
        self.url: str = data.get("url", "")
        self.title: str = data.get("title", "")
        self.ready_state: str = data.get("readyState", "")
        self.scroll_y: int = data.get("scrollY", 0)
        self.page_height: int = data.get("pageHeight", 0)
        self.viewport_height: int = data.get("viewportHeight", 0)
        self.elements: List[Dict[str, Any]] = data.get("elements", [])
        self.page_text: str = data.get("page_text", "")
        self.flags: Dict[str, bool] = data.get("flags", {})
        self.headings: List[str] = data.get("headings", [])
        self.notices: List[Dict[str, Any]] = data.get("notices", [])
        self.field_context: List[Dict[str, Any]] = data.get("field_context", [])
        self.dialog: Dict[str, Any] = data.get("dialog", {"open": False, "count": 0, "text": ""})
        self.game: Dict[str, Any] = data.get("game", {})

    def at_bottom(self) -> bool:
        return self.scroll_y + self.viewport_height >= self.page_height - 50

    def find_element(self, eid: str) -> Optional[Dict[str, Any]]:
        for el in self.elements:
            if el.get("eid") == eid:
                return el
        return None

    def to_llm_text(self, max_elements: int = 60) -> str:
        """Компактное текстовое представление для промпта LLM"""
        lines = [
            f"URL: {self.url}",
            f"Заголовок: {self.title}",
            f"Прокрутка: {self.scroll_y}/{self.page_height}px" + (" (внизу страницы)" if self.at_bottom() else ""),
        ]

        if self.headings:
            lines.append("Заголовки страницы: " + " | ".join(self.headings[:6]))

        if self.game.get("detected"):
            level = self.game.get("level")
            name = self.game.get("name") or "неизвестный уровень"
            lines.append(f"🎮 ИГРА NEAL.FUN: уровень {level or '?'} — {name}")
            lines.append(f"Игровых canvas: {len(self.game.get('canvases', []))}; интерактивных областей: {self.game.get('interactive_count', 0)}")

        if self.flags.get("captcha"):
            lines.append(" На странице CAPTCHA")
        if self.flags.get("cookie_banner"):
            lines.append("🍪 Обнаружен cookie-баннер (найди кнопку accept и кликни)")
        if self.flags.get("login_form"):
            lines.append("🔐 На странице есть форма входа (input[type=password])")
        if self.dialog.get("open"):
            lines.append(f"🪟 ОТКРЫТО МОДАЛЬНОЕ ОКНО ({self.dialog.get('count')}): {self.dialog.get('text', '')[:300]}")
            if self.dialog.get("blocking"):
                why = []
                if self.dialog.get("has_proceed"):
                    why.append("есть кнопка подтверждения")
                if self.dialog.get("has_required_field"):
                    why.append("есть обязательное поле")
                lines.append(
                    "   ⛔ БЛОКИРУЮЩЕЕ ОКНО (" + ", ".join(why) + "): оно требует действия. "
                    "Закрытие крестиком/«Сбросить»/Esc/«Не сейчас» НЕ подходит — "
                    "окно откроется снова. Нажми кнопку подтверждения (Сохранить и продолжить)."
                )
            btns = self.dialog.get("buttons") or []
            if btns:
                lines.append("   Кнопки окна: " + ", ".join(
                    f"[{b['eid']}] «{b['text']}»" for b in btns[:10]
                ))
        if self.notices:
            lines.append(" ВЫДЕЛЕННЫЕ СООБЩЕНИЯ НА СТРАНИЦЕ (цвет/роль — оцени семантику сам):")
            for n in self.notices:
                color = n.get("color", "")
                role = f", role={n['role']}" if n.get("role") else ""
                lines.append(f"   • [{color}{role}] {n['text'][:200]}")
        if self.field_context:
            lines.append("📝 КОНТЕКСТ ПОЛЕЙ ВВОДА (маска, текущее значение, подписи, подсказки):")
            for fc in self.field_context:
                bits = []
                if fc.get("mask"):
                    bits.append(f"маска/placeholder: «{fc['mask']}»")
                if fc.get("current_value"):
                    bits.append(f"уже введено: «{fc['current_value']}»")
                if fc.get("label"):
                    bits.append(f"подпись: «{fc['label']}»")
                if fc.get("text"):
                    bits.append(f"[{fc.get('color', '')}] {fc['text'][:150]}")
                if bits:
                    lines.append(f"   • поле {fc.get('eid')}: " + "; ".join(bits))

        lines.append("")
        lines.append("=== ИНТЕРАКТИВНЫЕ ЭЛЕМЕНТЫ (id: описание) ===")

        for el in self.elements[:max_elements]:
            parts = [f"[{el['eid']}]"]
            parts.append(f"<{el['tag']}>")
            if el.get("type"):
                parts.append(f"type={el['type']}")
            if el.get("text"):
                parts.append(f'"{el["text"]}"')
            if el.get("placeholder"):
                parts.append(f'placeholder="{el["placeholder"]}"')
            if el.get("name"):
                parts.append(f"name={el['name']}")
            if el.get("href"):
                parts.append(f"href={el['href']}")
            if el.get("x") is not None and el.get("y") is not None:
                parts.append(f"center=({el['x']},{el['y']})")
            if el.get("width") and el.get("height"):
                parts.append(f"size={el['width']}x{el['height']}")
            if el.get("cursor") == "pointer":
                parts.append("cursor=pointer")
            if el.get("img"):
                parts.append(f"img={el['img']}")
            if el.get("colors"):
                c = el["colors"]
                top = sorted(c.items(), key=lambda kv: -kv[1])[:3]
                parts.append("цвета:" + ",".join(f"{k}={v}%" for k, v in top if v > 5))
            if el.get("selected"):
                parts.append("[ВЫБРАНО]")
            if el.get("form"):
                parts.append(f"form={el['form']}")
            if el.get("value") and el.get("tag") in ("input", "textarea"):
                parts.append(f'value="{el["value"]}"')  # заполненные поля видны явно
            if el.get("required"):
                parts.append("*required")
            if el.get("disabled"):
                parts.append("[disabled]")
            if el.get("checked"):
                parts.append("[checked]")
            if el.get("in_dialog"):
                parts.append("(в модальном окне)")
            if not el.get("in_viewport"):
                parts.append("(вне экрана, нужен scroll)")
            lines.append(" ".join(parts))

        if len(self.elements) > max_elements:
            lines.append(f"... и ещё {len(self.elements) - max_elements} элементов (прокрути страницу)")

        lines.append("")
        lines.append("=== ТЕКСТ СТРАНИЦЫ (фрагмент) ===")
        lines.append(self.page_text[:2000])

        return "\n".join(lines)


async def capture_browser_state(page) -> BrowserState:
    """
    Захват BrowserState со страницы Playwright.

    ВАЖНО: обходит ВСЕ фреймы, включая кросс-origin (Ozon ID, виджеты оплаты,
    reCAPTCHA и т.п.) — через Playwright frames API, которому same-origin policy
    не мешает. Элементы фреймов получают продолжение нумерации eid (eN+1...),
    а data-eid ставится в DOM фрейма — click_element/type_into_element работают
    с ними через frame locator.
    """
    data = await page.evaluate(STATE_JS, 1)  # нумерация с e1
    elements = list(data.get("elements", []))
    counter = 1 + len(elements)

    # Кросс-origin и дочерние фреймы
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            frame_data = await frame.evaluate(STATE_JS, counter)
        except Exception as e:
            logger.debug(f"Frame {frame.url[:60]} skipped: {e}")
            continue
        frame_elements = frame_data.get("elements", [])
        if not frame_elements:
            continue
        # Пометка фрейма для последующих действий
        frame_url = (frame.url or "")[:100]
        for el in frame_elements:
            el["frame_url"] = frame_url
        elements.extend(frame_elements)
        counter += len(frame_elements)
        # Текст фрейма добавляем к page_text
        ftext = frame_data.get("page_text", "")
        if ftext:
            data["page_text"] = (data.get("page_text", "")) + "\n--- [содержимое фрейма " + frame_url[:60] + "] ---\n" + ftext[:1500]
        # Флаги из фрейма (captcha часто в iframe)
        fflags = frame_data.get("flags", {})
        for k, v in fflags.items():
            if v:
                data.setdefault("flags", {})[k] = True
        # Заметные сообщения и контекст полей из фрейма (Ozon ID и т.п.)
        for key in ("notices", "field_context"):
            fitems = frame_data.get(key, [])
            if fitems:
                data.setdefault(key, []).extend(fitems)
        # Dialog из фрейма
        fdialog = frame_data.get("dialog", {})
        if fdialog.get("open") and not data.get("dialog", {}).get("open"):
            data["dialog"] = fdialog

    data["elements"] = elements
    return BrowserState(data)
