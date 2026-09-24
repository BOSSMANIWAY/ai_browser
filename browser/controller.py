"""
Browser Controller v2 - Управление Chrome через Playwright/CDP
Единая стратегия подключения: connect_over_cdp → fallback launch.
"""

import asyncio
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TabInfo:
    """Информация о вкладке"""
    tab_id: str
    url: str
    title: str
    favicon: Optional[str] = None
    is_active: bool = False


class BrowserController:
    """
    Контроллер браузера v2.

    Стратегия подключения (одна, без конфликтов):
    1. Пытаемся connect_over_cdp к уже запущенному Chrome (порт 9222)
    2. Если не удалось — запускаем свой экземпляр Chrome через launch()
       (НЕ launch_persistent_context — он конфликтует с занятым профилем)

    Все вкладки берутся из browser.contexts[*].pages.
    tab_id = стабильный индекс страницы в контексте (пересчитывается при refresh).
    """

    def __init__(self, cdp_port: int = 9222):
        self.cdp_port = cdp_port
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._tabs: List[TabInfo] = []
        self._tab_map: Dict[str, Any] = {}  # tab_id -> Page
        self._is_persistent = False  # True если запущены через launch_persistent_context
        self.connected = False

    # ─── Подключение ────────────────────────────────────────────────

    async def connect(self, host: str = "127.0.0.1", port: Optional[int] = None,
                      mode: str = "auto") -> bool:
        """
        Подключение к Chrome.

        mode:
          - "attach"  — только подключение к существующему браузеру (CDP :9222)
          - "launch"  — только запуск управляемого Chrome с persistent-профилем
          - "auto"    — attach, при неудаче launch (по умолчанию)

        Возвращает True если подключились к существующему браузеру.
        """
        from playwright.async_api import async_playwright
        from pathlib import Path

        port = port or self.cdp_port
        self._playwright = await async_playwright().start()

        if mode in ("auto", "attach"):
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    f"http://{host}:{port}", timeout=5000
                )
                self.connected = True
                await self._pick_active_page()
                await self.refresh_tabs()
                logger.info(f"Connected to existing Chrome via CDP :{port}")
                return True
            except Exception as e:
                if mode == "attach":
                    logger.error(f"Attach mode failed: {e}")
                    await self.disconnect()
                    raise
                logger.info(f"CDP connect failed ({e}), launching managed Chrome")

        # launch: управляемый Chrome с постоянным профилем (куки сохраняются)
        profile_dir = Path(__file__).parent.parent / "ai_browser_profile"
        profile_dir.mkdir(exist_ok=True)

        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                channel="chrome",
                viewport={"width": 1440, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-infobars",
                ],
            )
            self.connected = True
            self._is_persistent = True
            self._browser = self._context.browser
            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
            await self.refresh_tabs()
            logger.info(f"Launched Chrome with persistent profile: {profile_dir}")
            return False
        except Exception as e:
            logger.error(f"Failed to launch Chrome: {e}")
            await self.disconnect()
            raise

    async def disconnect(self):
        """Отключение. Закрываем свой экземпляр, CDP-подключение просто отвязываем."""
        try:
            if self._context and self._is_persistent:
                await self._context.close()
            elif self._browser:
                # connect_over_cdp: close() отвязывается, не убивая Chrome
                await self._browser.close()
        except Exception as e:
            logger.warning(f"Browser close warning: {e}")
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning(f"Playwright stop warning: {e}")
        self._browser = None
        self._context = None
        self._playwright = None
        self._page = None
        self.connected = False
        logger.info("Disconnected from Chrome")

    # ─── Вкладки ────────────────────────────────────────────────────

    def _all_pages(self) -> List[Any]:
        """Все страницы: из persistent context или из всех контекстов браузера"""
        if self._context and self._is_persistent:
            return list(self._context.pages)
        if not self._browser:
            return []
        pages = []
        for ctx in self._browser.contexts:
            pages.extend(ctx.pages)
        return pages

    async def _pick_active_page(self):
        """Выбрать активную страницу: последняя открытая непустая"""
        pages = self._all_pages()
        if not pages:
            ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            self._page = await ctx.new_page()
            return
        # Предпочитаем страницу с реальным URL (не about:blank)
        for p in reversed(pages):
            if p.url and not p.url.startswith("about:"):
                self._page = p
                return
        self._page = pages[-1]

    async def refresh_tabs(self) -> List[TabInfo]:
        """Пересобрать список вкладок и стабильную карту tab_id -> Page"""
        self._tabs = []
        self._tab_map = {}
        for i, page in enumerate(self._all_pages()):
            tab_id = f"tab{i}"
            try:
                title = await page.title()
            except Exception:
                title = ""
            self._tabs.append(TabInfo(
                tab_id=tab_id,
                url=page.url,
                title=title,
                is_active=(page == self._page),
            ))
            self._tab_map[tab_id] = page
        return self._tabs

    async def get_tabs(self) -> List[TabInfo]:
        return await self.refresh_tabs()

    async def new_tab(self, url: Optional[str] = None) -> TabInfo:
        """Открыть новую вкладку"""
        if self._context and self._is_persistent:
            page = await self._context.new_page()
        else:
            ctx = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
            page = await ctx.new_page()
        if url:
            await page.goto(url, wait_until="domcontentloaded")
        self._page = page
        await self.refresh_tabs()
        logger.info(f"New tab: {url or 'blank'}")
        return TabInfo(tab_id=self._find_tab_id(page), url=page.url, title=await page.title(), is_active=True)

    async def switch_tab(self, tab_id: str):
        """Переключиться на вкладку по tab_id"""
        page = self._tab_map.get(tab_id)
        if not page:
            await self.refresh_tabs()
            page = self._tab_map.get(tab_id)
        if not page:
            raise ValueError(f"Tab not found: {tab_id}")
        self._page = page
        try:
            await page.bring_to_front()
        except Exception:
            pass
        await self.refresh_tabs()
        logger.info(f"Switched to tab {tab_id}: {page.url}")

    async def close_tab(self, tab_id: str):
        """Закрыть вкладку"""
        page = self._tab_map.get(tab_id)
        if not page:
            raise ValueError(f"Tab not found: {tab_id}")
        await page.close()
        if self._page is page:
            pages = self._all_pages()
            self._page = pages[0] if pages else None
        await self.refresh_tabs()
        logger.info(f"Closed tab {tab_id}")

    def _find_tab_id(self, page: Any) -> Optional[str]:
        for tid, p in self._tab_map.items():
            if p is page:
                return tid
        return None

    # ─── Действия ───────────────────────────────────────────────────

    @property
    def page(self) -> Optional[Any]:
        return self._page

    def _require_page(self) -> Any:
        if not self._page:
            raise RuntimeError("Browser not connected")
        return self._page

    async def navigate(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000) -> Dict[str, Any]:
        """
        Навигация. Возвращает результат:
        {"ok": True, "url": ...} или {"ok": False, "error": "ERR_NAME_NOT_RESOLVED: ..."}
        DNS/сетевые ошибки — это НЕ успех.
        """
        page = self._require_page()
        if not url.startswith(("http://", "https://", "about:", "file://")):
            url = "https://" + url
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as e:
            err_str = str(e)
            # Извлекаем код ошибки (ERR_NAME_NOT_RESOLVED, ERR_CONNECTION_REFUSED, ...)
            import re
            m = re.search(r"(net::ERR_[A-Z_]+)", err_str)
            err_code = m.group(1) if m else "NAVIGATION_FAILED"
            logger.warning(f"Navigate failed ({url}): {err_code}")
            await self.refresh_tabs()
            return {"ok": False, "error": f"{err_code}: {url}", "url": url}
        await self.refresh_tabs()
        logger.info(f"Navigated to: {url}")
        return {"ok": True, "url": url}

    async def _frame_for_element(self, element_id: str):
        """
        Найти фрейм, в котором лежит элемент с данным data-eid.
        Возвращает Playwright Frame или None (элемент в главном документе).
        Каждая проверка фрейма ограничена по времени — зависший evaluate
        не должен блокировать перебор.
        """
        page = self._require_page()
        import asyncio as _aio
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                found = await _aio.wait_for(
                    frame.evaluate(
                        '(eid) => !!document.querySelector(`[data-eid="${eid}"]`)', element_id
                    ), timeout=3
                )
                if found:
                    return frame
            except Exception:
                continue
        return None

    async def click_element(self, element_id: str) -> Dict[str, Any]:
        """Клик по элементу из BrowserState с реальными pointer-событиями."""
        page = self._require_page()
        import asyncio as _aio

        async def _native_click():
            frame2 = await self._frame_for_element(element_id)
            target = frame2.locator(f'[data-eid="{element_id}"]') if frame2 is not None else page.locator(f'[data-eid="{element_id}"]')
            await target.scroll_into_view_if_needed()
            await target.click(timeout=5000)

        try:
            # Жёсткий лимит: CDP-клик может зависнуть навсегда и заблокировать
            # весь цикл агента (наблюдали на neal.fun) — вместо бесконечного
            # ожидания падаем в JS-fallback.
            await _aio.wait_for(_native_click(), timeout=12)
            return {"ok": True}
        except Exception as exc:
            logger.debug("Native click failed for %s: %s", element_id, exc)
            try:
                frame = await _aio.wait_for(self._frame_for_element(element_id), timeout=5)
                evaluator = frame.evaluate if frame is not None else page.evaluate
                return await _aio.wait_for(evaluator("""
                (eid) => {
                    const el = document.querySelector(`[data-eid="${eid}"]`);
                    if (!el) return {ok: false, error: 'element not found: ' + eid};
                    el.scrollIntoView({block: 'center', behavior: 'instant'});
                    const r = el.getBoundingClientRect();
                    const opts = {bubbles: true, cancelable: true, clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
                    el.dispatchEvent(new PointerEvent('pointerdown', opts));
                    el.dispatchEvent(new MouseEvent('mousedown', opts));
                    el.dispatchEvent(new MouseEvent('mouseup', opts));
                    el.dispatchEvent(new PointerEvent('pointerup', opts));
                    el.click();
                    return {ok: true, fallback: true, tag: el.tagName};
                }
            """, element_id), timeout=8)
            except Exception as exc2:
                return {"ok": False, "error": f"click failed: {exc2}"}

    async def click_at(self, x: float, y: float) -> Dict[str, Any]:
        """Координатный клик для canvas/игровых областей."""
        page = self._require_page()
        # Бесконечные зависания mouse.click на игровых canvas: делаем через
        # asyncio.wait_for, чтобы шаг не блокировал цикл агента навсегда.
        import asyncio as _aio
        try:
            await _aio.wait_for(page.mouse.click(x, y), timeout=8)
        except _aio.TimeoutError:
            logger.warning(f"click_at timeout ({x},{y}) — fallback на dispatchEvent")
            await page.evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y) || document.body;
                    const r = el.getBoundingClientRect();
                    const opts = {bubbles: true, cancelable: true,
                                  clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
                    el.dispatchEvent(new PointerEvent('pointerdown', opts));
                    el.dispatchEvent(new MouseEvent('mousedown', opts));
                    el.dispatchEvent(new MouseEvent('mouseup', opts));
                    el.dispatchEvent(new PointerEvent('pointerup', opts));
                    el.click();
                }""",
                [x, y],
            )
        return {"ok": True, "x": x, "y": y}

    async def hover_at(self, x: float, y: float) -> Dict[str, Any]:
        page = self._require_page()
        await page.mouse.move(x, y)
        return {"ok": True, "x": x, "y": y}

    async def drag_at(self, x1: float, y1: float, x2: float, y2: float, steps: int = 12) -> Dict[str, Any]:
        page = self._require_page()
        import asyncio as _aio

        async def _do():
            await page.mouse.move(x1, y1)
            await page.mouse.down()
            await page.mouse.move(x2, y2, steps=steps)
            await page.mouse.up()

        try:
            await _aio.wait_for(_do(), timeout=15)
        except _aio.TimeoutError:
            return {"ok": False, "error": f"drag завис ({x1},{y1}→{x2},{y2})"}
        return {"ok": True, "from": [x1, y1], "to": [x2, y2]}

    async def get_dialog_state(self) -> Dict[str, Any]:
        """Состояние модальных окон/dialog: открыты ли, какие элементы внутри"""
        page = self._require_page()
        try:
            return await page.evaluate("""
                () => {
                    const dialogs = [...document.querySelectorAll(
                        'dialog[open], [role=dialog], [role=alertdialog], .modal.show, .modal[style*="display: block"], [class*="modal"][class*="open"], [class*="popup"][style*="display: block"], [class*="Modal"]'
                    )].filter(d => {
                        const r = d.getBoundingClientRect();
                        const s = getComputedStyle(d);
                        return r.width > 50 && r.height > 50 && s.display !== 'none' && s.visibility !== 'hidden';
                    });
                    return {
                        open: dialogs.length > 0,
                        count: dialogs.length,
                        inputs: dialogs.flatMap(d => [...d.querySelectorAll('input, button, textarea')]).length,
                        text: dialogs.map(d => (d.innerText || '').substring(0, 200)).join(' | ')
                    };
                }
            """)
        except Exception:
            return {"open": False, "count": 0, "inputs": 0, "text": ""}

    async def type_into_element(self, element_id: str, text: str, clear: bool = True) -> Dict[str, Any]:
        """Надёжно заменить/ввести текст в input, textarea или managed-editor."""
        page = self._require_page()
        is_code_page = "/code/" in (page.url or "") or "assessment.hh.ru" in (page.url or "")
        frame = await self._frame_for_element(element_id)
        if is_code_page:
            # e10 размечается внутри iframe. Ищем textarea в том же frame,
            # иначе page.keyboard будет печатать в главном документе.
            root = frame
            if root is None:
                for candidate in page.frames:
                    try:
                        if await candidate.locator("textarea").count():
                            root = candidate
                            break
                    except Exception:
                        continue
            root = root if root is not None else page
            target = root.locator("textarea").first
        else:
            root = frame if frame is not None else page
            target = root.locator(f'[data-eid="{element_id}"]').first
        try:
            if is_code_page and clear:
                api = await self._set_code_editor_value(root, text)
                if api.get("ok"):
                    want = text.replace("\r\n", "\n")
                    if api.get("value") == want:
                        api["verified_on_same_locator"] = True
                        return api
                    logger.info(
                        "code-editor API вернул другое значение (len=%s, ожидалось %s)",
                        len(api.get("value") or ""), len(want),
                    )
                else:
                    logger.info(
                        "code-editor API не подтверждён (%s) — пробую клавиатуру",
                        api.get("tried"),
                    )
            # На assessment.hh.ru textarea постоянно пересоздаётся после input.
            # Стабильный селектор textarea надёжнее временного data-eid.
            if await target.count() == 0:
                target = root.locator("textarea").first
            await target.scroll_into_view_if_needed()
            await target.click(timeout=5000)
            if clear:
                await target.press("ControlOrMeta+A")
                await target.press("Backspace")
            is_editable = await target.get_attribute("contenteditable") == "true"
            if is_editable:
                await root.keyboard.insert_text(text)
                actual = await target.inner_text()
            else:
                # Keyboard input корректно обновляет React/Vue-редакторы и их
                # внутреннее состояние, в отличие от прямого присваивания value.
                if clear:
                    await root.keyboard.insert_text(text)
                else:
                    await target.type(text, delay=1)
                actual = await target.input_value()
            if clear and actual != text:
                # После события input узел мог быть заменён — перечитываем новое
                # textarea, но не считаем старый eID обязательным.
                fresh = root.locator("textarea").first if is_code_page else root.locator(f'[data-eid="{element_id}"]').first
                if await fresh.count():
                    actual = await fresh.input_value()
            if clear and actual != text:
                return {"ok": False, "error": "значение редактора не совпало после ввода", "actual": actual}
            return {"ok": True, "value": actual, "verified_on_same_locator": True}
        except Exception as e:
            # Fallback с native value setter нужен для React/Vue-контролируемых полей.
            js = """
                ([eid, value, replace]) => {
                    const el = document.querySelector(`[data-eid="${eid}"]`);
                    if (!el) return {ok: false, error: 'element not found'};
                    el.focus();
                    if (el.isContentEditable) el.innerText = replace ? value : el.innerText + value;
                    else {
                        const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, replace ? value : el.value + value);
                    }
                    el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText'}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return {ok: true, fallback: true, value: el.value !== undefined ? el.value : el.innerText};
                }
            """
            if is_code_page:
                target = root.locator("textarea").first
                if await target.count():
                    await target.fill(text if clear else ((await target.input_value()) + text))
                    actual = await target.input_value()
                    return {"ok": actual == text if clear else True, "value": actual,
                            "verified_on_same_locator": actual == text if clear else True}
            fallback_result = await root.evaluate(js, [element_id, text, clear])
            if fallback_result.get("ok") and clear and fallback_result.get("value") == text:
                fallback_result["verified_on_same_locator"] = True
            return fallback_result

    async def _code_editor_roots(self, root):
        """Frame'ы, в которых стоит искать модель редактора кода."""
        roots = []
        candidates = [root] + list(getattr(self._page, "frames", []) or [])
        for cand in candidates:
            if cand is None or any(cand is seen for seen in roots):
                continue
            roots.append(cand)
        return roots

    async def _eval_in_code_roots(self, js: str, arg):
        """Выполнить js в каждом candidate-frame, вернуть (result, tried)."""
        tried: list = []
        for cand in await self._code_editor_roots(None):
            try:
                evaluator = cand.evaluate if hasattr(cand, "evaluate") else self._page.evaluate
                res = await asyncio.wait_for(evaluator(js, arg), timeout=8)
            except Exception as exc:
                tried.append("eval-err:%s" % exc)
                continue
            if isinstance(res, dict) and res.get("ok"):
                return res, tried
            if isinstance(res, dict):
                tried.extend(res.get("tried") or [])
        return None, tried[:8]

    async def _set_code_editor_value(self, root, text: str) -> Dict[str, Any]:
        """Поставить код через API редактора (Monaco / CodeMirror 5 / CodeMirror 6).

        На assessment.hh.ru видимое <textarea> — скрытый прокси: редактор
        очищает его после события input, поэтому проверка input_value() всегда
        даёт расхождение и решение фактически не подставляется. Надёжный
        способ — писать в модель самого редактора.
        """
        js = """
            (text) => {
                const out = {ok: false, tried: []};
                try {
                    const m = window.monaco;
                    if (m && m.editor) {
                        const models = m.editor.getModels();
                        if (models && models.length) {
                            models[0].setValue(text);
                            return {ok: true, api: 'monaco', value: models[0].getValue()};
                        }
                        const eds = (m.editor.getEditors && m.editor.getEditors()) || [];
                        if (eds.length) {
                            eds[0].setValue(text);
                            return {ok: true, api: 'monaco-editor', value: eds[0].getValue()};
                        }
                        out.tried.push('monaco:no-models');
                    } else { out.tried.push('no-monaco'); }
                } catch (e) { out.tried.push('monaco-err:' + e.message); }
                try {
                    const cm = document.querySelector('.CodeMirror');
                    if (cm && cm.CodeMirror) {
                        cm.CodeMirror.setValue(text);
                        return {ok: true, api: 'cm5', value: cm.CodeMirror.getValue()};
                    }
                    out.tried.push(cm ? 'cm5:no-instance' : 'no-cm5');
                } catch (e) { out.tried.push('cm5-err:' + e.message); }
                try {
                    const content = document.querySelector('.cm-content');
                    if (content) {
                        let view = null;
                        if (content.cmView) view = content.cmView.view || null;
                        if (!view) {
                            const host = content.closest('.cm-editor') || content.parentElement;
                            if (host && host.cmView) view = host.cmView.view || null;
                        }
                        if (view && view.dispatch && view.state) {
                            view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: text}});
                            return {ok: true, api: 'cm6', value: view.state.doc.toString()};
                        }
                        out.tried.push('cm6:no-view');
                    } else { out.tried.push('no-cm6'); }
                } catch (e) { out.tried.push('cm6-err:' + e.message); }
                return out;
            }
        """
        res, tried = await self._eval_in_code_roots(js, text)
        if not res:
            return {"ok": False, "tried": tried}
        value = (res.get("value") or "").replace("\r\n", "\n")
        res["value"] = value
        res["detail"] = "код установлен через %s" % res.get("api")
        return res

    async def _read_code_editor_value(self, root) -> Optional[str]:
        """Прочитать содержимое редактора кода из его модели."""
        js = """
            () => {
                try {
                    const m = window.monaco;
                    if (m && m.editor) {
                        const models = m.editor.getModels();
                        if (models && models.length) {
                            return {ok: true, value: models[0].getValue()};
                        }
                    }
                } catch (e) {}
                try {
                    const cm = document.querySelector('.CodeMirror');
                    if (cm && cm.CodeMirror) {
                        return {ok: true, value: cm.CodeMirror.getValue()};
                    }
                } catch (e) {}
                try {
                    const lines = document.querySelectorAll('.cm-content .cm-line');
                    if (lines.length) {
                        // ВАЖНО: это Python-строка, а не raw — '\n' здесь уже
                        // реальный перевод строки, который ломает JS-литерал.
                        const txt = [...lines].map(l => l.textContent).join(String.fromCharCode(10));
                        if (txt.trim()) return {ok: true, value: txt};
                    }
                } catch (e) {}
                return {ok: false};
            }
        """
        res, _ = await self._eval_in_code_roots(js, None)
        if not res:
            return None
        value = (res.get("value") or "").replace("\r\n", "\n")
        return value or None

    def _is_code_page(self) -> bool:
        url = (self._page.url or "") if self._page else ""
        return "/code/" in url or "assessment.hh.ru" in url

    async def _read_element_value(self, root, element_id: str) -> Optional[str]:
        """Читать значение тем же frame/root, в котором выполнялся ввод."""
        try:
            target = (root.locator("textarea").first
                      if "/code/" in (self._page.url or "") or "assessment.hh.ru" in (self._page.url or "")
                      else root.locator(f'[data-eid="{element_id}"]').first)
            if await target.get_attribute("contenteditable") == "true":
                return await target.inner_text()
            value = await target.input_value()
            if self._is_code_page() and not (value or "").strip():
                # На hh.ru textarea — скрытый прокси редактора: реальное
                # содержимое живёт в модели Monaco/CodeMirror.
                via_api = await self._read_code_editor_value(root)
                if via_api:
                    return via_api
            return value
        except Exception:
            return None

    async def get_element_value(self, element_id: str) -> Optional[str]:
        """Текущее значение поля, включая iframe и contenteditable."""
        page = self._require_page()
        frame = await self._frame_for_element(element_id)
        return await self._read_element_value(frame if frame is not None else page, element_id)

    async def select_option(self, element_id: str, value: str) -> Dict[str, Any]:
        """Выбор опции в select по element_id"""
        page = self._require_page()
        try:
            await page.select_option(f'[data-eid="{element_id}"]', value=value)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def press_key(self, key: str):
        """Нажатие клавиши"""
        page = self._require_page()
        await page.keyboard.press(key)

    async def scroll(self, direction: str = "down", amount: int = 600):
        """Прокрутка"""
        page = self._require_page()
        delta = amount if direction == "down" else -amount
        await page.mouse.wheel(0, delta)

    async def extract_text(self, selector: str = "body", limit: int = 5000) -> str:
        """Извлечение текста"""
        page = self._require_page()
        try:
            text = await page.inner_text(selector, timeout=5000)
            return text[:limit]
        except Exception:
            return ""

    async def evaluate_js(self, expression: str) -> Any:
        """Выполнение JS (fallback-механизм, использовать только когда tools не хватает)"""
        page = self._require_page()
        return await page.evaluate(expression)

    async def screenshot(self, full_page: bool = False, quality: int = 70) -> Optional[str]:
        """Скриншот в base64 (jpeg). None при ошибке."""
        page = self._require_page()
        try:
            raw = await page.screenshot(full_page=full_page, type="jpeg", quality=quality)
            import base64
            return base64.b64encode(raw).decode("utf-8")
        except Exception as e:
            logger.warning(f"Screenshot failed: {e}")
            return None

    async def wait_for(self, seconds: float):
        await asyncio.sleep(seconds)

    async def get_page_metrics(self) -> Dict[str, Any]:
        """Метрики страницы (для verify после действий)"""
        page = self._require_page()
        try:
            return await page.evaluate("""
                () => ({
                    url: window.location.href,
                    title: document.title,
                    domElements: document.querySelectorAll('*').length,
                    forms: document.querySelectorAll('form').length,
                    inputs: document.querySelectorAll('input').length,
                    readyState: document.readyState
                })
            """)
        except Exception:
            return {}

    async def wait_for_stable_page(self, timeout: float = 10.0) -> bool:
        """
        Дождаться стабильности страницы после навигации/клика.
        Ждёт document.readyState == 'complete' и отсутствия активной навигации.
        Возвращает True если страница стабильна.
        """
        import asyncio as _asyncio
        page = self._page
        if not page:
            return False

        deadline = _asyncio.get_event_loop().time() + timeout
        while _asyncio.get_event_loop().time() < deadline:
            try:
                state = await page.evaluate(
                    "() => ({rs: document.readyState, url: window.location.href})"
                )
                if state.get("rs") == "complete":
                    return True
                # loading/interactive — ждём ещё
                await _asyncio.sleep(0.3)
            except Exception:
                # Execution context destroyed — страница перезагружается, ждём
                await _asyncio.sleep(0.5)

        logger.warning(f"wait_for_stable_page: timeout after {timeout}s")
        return False