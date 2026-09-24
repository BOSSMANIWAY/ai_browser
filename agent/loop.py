"""
Agent Loop v2.1 - Comet-подобный цикл: observe → decide → act → verify → goal-check

Ключевые механики:
- GoalDetector: runtime определяет успех по объективным сигналам (URL-параметры
  формы, success-сообщения) — LLM не нужно самому решать "задача выполнена?"
- State-based loop breaker: same state + same action + no progress → LLM НЕ
  спрашивается повторно, вместо этого применяется recovery-стратегия
- LM retry: пустой ответ → retry, а не искусственный wait
"""

import asyncio
import base64
import logging
import re
import subprocess
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional

from agent.memory import AgentMemory
from agent.planner import AgentPlanner
from agent.goal_detector import GoalDetector
from agent.state_diff import diff_states, progress_verdict, verify_typed_value
from browser.controller import BrowserController
from browser.browser_state import capture_browser_state, BrowserState

logger = logging.getLogger(__name__)

# ─── Game sheet (vision для визуальных уровней not-a-robot) ─────────────
# Клетки уровня — DIV с background-image (спрайт) + background-position %.
# Кроп каждого спрайта собирается в canvas: одна картинка-«простыня» с
# подписями eID. Закон доставки pplx: PNG доходит нестабильно (флап ~20-70%),
# поэтому обязателен retry с верификацией ответа.
GAME_SHEET_JS = r"""async (tile) => {
    const cells = [];
    for (const el of document.querySelectorAll('[data-eid]')) {
        const st = el.getAttribute('style') || '';
        if (!st.includes('background-image')) continue;
        const mp = st.match(/background-position:\s*([\d.]+)%\s+([\d.]+)%/);
        const mi = st.match(/background-image:\s*url\(["']?([^"')]+)["']?\)/);
        const ms = st.match(/background-size:\s*([\d.]+)%\s+([\d.]+)%/);
        if (!mp || !mi) continue;
        cells.push({id: el.getAttribute('data-eid'), x: parseFloat(mp[1]), y: parseFloat(mp[2]),
                    img: new URL(mi[1], location.href).href,
                    sx: ms ? parseFloat(ms[1]) : 400, sy: ms ? parseFloat(ms[2]) : 400});
    }
    if (!cells.length) return {error: 'no cells'};
    const uniq = [...new Set(cells.map(c => c.img))];
    const imgs = {};
    for (const u of uniq) {
        try {
            const resp = await fetch(u);
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const im = new Image();
            im.src = url;
            await new Promise((res, rej) => { im.onload = res; im.onerror = rej; });
            imgs[u] = im;
        } catch (e) { return {error: 'sprite load fail: ' + u}; }
    }
    // Порядок клеток: по строкам (y), внутри строки по x — как видит человек
    const byRow = {};
    for (const c of cells) (byRow[c.y + '|' + c.img] = byRow[c.y + '|' + c.img] || []).push(c);
    const rows = Object.values(byRow).map(r => r.sort((a, b) => a.x - b.x));
    rows.sort((r1, r2) => r1[0].y - r2[0].y);
    const LBL = Math.max(16, Math.round(tile * 0.36));
    const cv = document.createElement('canvas');
    cv.width = tile * rows[0].length;
    cv.height = rows.length * (tile + LBL);
    const ctx = cv.getContext('2d');
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, cv.width, cv.height);
    const order = [];
    rows.forEach((row, ri) => {
        row.forEach((cell, ci) => {
            const im = imgs[cell.img];
            const cols = Math.round(cell.sx / 100 * 4) || 4;
            const rowsN = Math.round(cell.sy / 100 * 4) || 4;
            const tw = im.naturalWidth / cols, th = im.naturalHeight / rowsN;
            const fx = cell.x / 100 * (im.naturalWidth - tw);
            const fy = cell.y / 100 * (im.naturalHeight - th);
            const dx = ci * tile, dy = ri * (tile + LBL);
            try { ctx.drawImage(im, fx, fy, tw, th, dx, dy, tile, tile); } catch (e) {}
            ctx.fillStyle = '#fff';
            ctx.font = 'bold ' + Math.round(LBL * 0.62) + 'px monospace';
            ctx.textAlign = 'center';
            ctx.fillText(cell.id, dx + tile / 2, dy + tile + LBL * 0.75);
            order.push(cell.id);
        });
    });
    // PNG до ~130KB b64 надёжен; при превышении — повтор с меньшим tile
    let b64 = cv.toDataURL('image/png').split(',')[1];
    return {b64, order, w: cv.width, h: cv.height};
}"""


async def _capture_game_sheet(controller, tile: int = 48) -> Dict[str, Any]:
    """Собрать sheet-картинку игрового уровня (PNG b64 + порядок eID)."""
    page = controller.page
    from browser.browser_state import STATE_JS
    await asyncio.wait_for(page.evaluate(STATE_JS, 1), timeout=10)
    r = await asyncio.wait_for(page.evaluate(GAME_SHEET_JS, tile), timeout=20)
    return r or {}


class AgentState(Enum):
    """Состояния автомата агента"""
    RUNNING = "running"            # обычный цикл observe→decide→act
    WAITING_USER = "waiting_user"  # пауза wait_user (SMS-код, 2FA)
    RECOVERING = "recovering"      # recovery после зацикливания
    COMPLETED = "completed"
    FAILED = "failed"


class AgentLoop:
    """Основной цикл агента v2.1"""

    def __init__(
        self,
        controller: BrowserController,
        memory: AgentMemory,
        llm_client: Any,
        max_steps: int = 40,
        model: str = "claude47opus",
        on_progress=None,
        on_finish=None,
    ):
        self.controller = controller
        self.memory = memory
        self.llm_client = llm_client
        self.max_steps = max_steps
        self.model = model
        self.task = ""
        self.planner = AgentPlanner(llm_client, model)
        self.goal_detector = GoalDetector()
        self.running = False
        self.step_count = 0
        self.state = AgentState.RUNNING
        self.on_progress = on_progress or (lambda text: asyncio.sleep(0))
        self.on_finish = on_finish or (lambda reason: asyncio.sleep(0))
        # Анти-луп: история (state_key, action_key)
        self._state_action_history: List[tuple] = []
        self._recovery_used = 0
        # Пинг-понг блокирующей модалки: сколько раз одно и то же окно
        # открылось заново после закрытия (агент жмёт крестик вместо «Сохранить»)
        self._dialog_open_counts: Dict[str, int] = {}
        self._dialog_prev_sig: Optional[str] = None
        self._dialog_hint_given: set = set()
        # После успешной записи кода запрещаем повторную печать в тот же редактор.
        # Это защищает от LLM-loop и сброса уже принятого решения.
        self._typed_editors: Dict[str, str] = {}
        self._rejected_credentials: Dict[str, str] = {}
        self._user_input_event: Optional[asyncio.Event] = None
        self._user_input_value: Optional[str] = None
        # Сообщения пользователя во время работы агента (код из SMS и т.п.)
        self._user_messages: List[str] = []

    def stop(self):
        """Остановка цикла агента"""
        self.running = False
        self.state = AgentState.FAILED
        logger.info("Agent loop stop requested")

    async def run(self, task: str):
        """Запуск агента с задачей"""
        self.task = task
        self.running = True
        self.step_count = 0
        self.state = AgentState.RUNNING
        self._state_action_history = []
        self._recovery_used = 0
        self._dialog_open_counts = {}
        self._dialog_prev_sig = None
        self._dialog_hint_given = set()
        self._typed_editors = {}
        self._rejected_credentials = {}
        self._user_input_event = None
        self._user_input_value = None
        self._user_messages = []
        self.memory.add_goal(task)

        logger.info(f"Agent loop started: {task[:100]}")
        await self._progress(f" Задача: {task}")

        last_result = "(первый шаг)"
        state_before: Optional[BrowserState] = None

        try:
            while self.running and self.step_count < self.max_steps:
                self.step_count += 1
                logger.info(f"Step {self.step_count}/{self.max_steps}")

                # 1. OBSERVE
                state = await self._observe()
                if state is None:
                    # Страница не отвечает (CDP-канал умер/Chrome закрыт) —
                    # не убиваем задачу: пробуем переподключиться к вкладке.
                    await self._progress("[WARN] Страница не отвечает — попытка переподключения")
                    try:
                        # refresh_tabs/title могут зависнуть на мёртвом канале —
                        # ограничиваем всю попытку общим таймаутом
                        async def _light_reconnect():
                            await self.controller.refresh_tabs()
                            await self.controller._pick_active_page()
                            return await self._observe()
                        state = await asyncio.wait_for(_light_reconnect(), timeout=25)
                    except Exception as e:
                        logger.warning(f"Reconnect attempt failed: {e}")
                if state is None:
                    # Chrome мог умереть целиком (TAL/краш) ИЛИ быть живым без
                    # CDP (запущен вручную) — revive различит оба случая.
                    state = await self._revive_chrome()
                if state is None:
                    # Последний шанс: CDP-канал сломан на уровне Playwright
                    # (page.evaluate висит при живом CDP). Полный реконнект.
                    logger.warning("Capture failed with responsive CDP — full Playwright reconnect")
                    try:
                        await self.controller.disconnect()
                        await self.controller.connect(mode="attach")
                        if "not-a-robot" in self.task:
                            await self.controller.navigate("https://neal.fun/not-a-robot/")
                            await asyncio.sleep(2)
                        state = await self._observe()
                    except Exception as e:
                        logger.error(f"Full reconnect failed: {e}")
                if state is None:
                    await self._progress("[ERROR] Не удалось получить состояние страницы")
                    break
                state_before = state

                # 1.3 БЛОКИРУЮЩЕЕ МОДАЛЬНОЕ ОКНО: то же окно открылось заново
                # после того, как агент его «закрыл» → подсказка вместо шага впустую.
                dialog_hint = self._track_dialog(state)
                if dialog_hint:
                    last_result = dialog_hint

                # 1.5 GOAL CHECK (state-only, ДО LLM): цель могла быть достигнута
                # без агента — пользователь сам ввёл код во время wait_user,
                # сессия уже авторизована, произошёл redirect. Runtime решает сам.
                goal_reason = self.goal_detector.check_state(task, state)
                if goal_reason:
                    self.state = AgentState.COMPLETED
                    await self._progress(f"[GOAL] Цель достигнута (runtime, без LLM): {goal_reason}")
                    self.memory.complete_goal(task)
                    self.memory.add_action("goal_check", {"goal_completed": True, "reason": goal_reason})
                    await self.on_finish(goal_reason)
                    break

                # 2. LOOP BREAKER: то же состояние + то же действие → recovery без LLM
                if self._is_stuck(state):
                    self.state = AgentState.RECOVERING
                    recovery = await self._apply_recovery(state)
                    if recovery == "stop":
                        self.state = AgentState.FAILED
                        break
                    last_result = recovery  # подсказка для LLM на следующем шаге
                    self.state = AgentState.RUNNING
                    continue

                # 3. DECIDE (с retry при пустом ответе)
                # Визуальные уровни игры: текстовое состояние не показывает
                # содержимое картинок — собираем sheet (кропы спрайтов с
                # подписями eID) и даём модели vision.
                screenshot_b64 = ""
                sheet_order = []
                if state.game.get("detected"):
                    screenshot_b64, sheet_order = await self._capture_vision_image()
                    if screenshot_b64:
                        logger.info(f"Game sheet captured: {len(screenshot_b64)} b64 chars, "
                                    f"{len(sheet_order)} cells")

                action = await self._decide_with_retry(
                    task, state, last_result,
                    rejected_warning=self._rejected_warning(),
                    user_messages=self._drain_user_messages(),
                    screenshot_b64=screenshot_b64,
                    sheet_order=sheet_order,
                )
                if action is None:
                    await self._progress("[ERROR] LLM недоступен после 3 попыток — остановка")
                    await self.on_finish("LLM не отвечает (пустые ответы после 3 retry)")
                    break

                tool = action.get("tool", "?")
                thought = action.get("thought", "")
                plan = action.get("plan", [])

                # LM может пометить значение как отвергнутое сайтом (её вердикт)
                if action.get("rejected_value"):
                    self.remember_rejected_value(
                        action["rejected_value"],
                        action.get("rejected_message") or thought,
                    )
                    await self._progress(f"   [REJECTED] Запомнено: {action['rejected_value']} отвергнут сайтом")

                await self._progress(f"[STEP] Шаг {self.step_count}: {tool}")
                if thought:
                    await self._progress(f"[THOUGHT] {thought[:200]}")
                if plan:
                    await self._progress(f"[PLAN] {' → '.join(plan[:5])}")

                # 4. ACT
                # 4.0 Анти-пинг-понг: отмена обязательного модального окна
                # запрещена. Дальше сработает обычный loop-breaker, если модель
                # продолжит настаивать на отмене.
                blocked = self._dialog_guard(action, state)
                if blocked is not None:
                    result = blocked
                elif tool == "type":
                    eid = action.get("element_id", "")
                    text = action.get("text", "")
                    previous = self._typed_editors.get(eid)
                    if previous is not None and previous == text:
                        result = {
                            "ok": False,
                            "error": f"редактор {eid} уже заполнен и проверен; повторная запись заблокирована — отправь решение",
                        }
                    elif previous is not None and state.url.startswith("https://assessment.hh.ru/code/"):
                        result = {
                            "ok": False,
                            "error": f"редактор {eid} уже изменён; повторная запись заблокирована до проверки решения",
                        }
                    else:
                        result = await self._execute(action, state)
                else:
                    result = await self._execute(action, state)

                # 4.5 Цель достигнута прямо в _execute (wait_user → пользователь
                # сам завершил вход). Runtime завершает БЕЗ LLM.
                if result.get("goal_completed"):
                    self.state = AgentState.COMPLETED
                    reason = result.get("goal_reason") or "Цель достигнута во время ожидания"
                    await self._progress(f"[GOAL] {reason}")
                    self.memory.complete_goal(task)
                    self.memory.add_action(tool, {"result": result, "goal_completed": True})
                    await self.on_finish(reason)
                    break

                # 9. Завершение по инициативе LLM (до observe — страница не менялась)
                if tool == "finish":
                    self.state = AgentState.COMPLETED
                    reason = action.get("reason") or action.get("text") or "Задача выполнена"
                    self.memory.complete_goal(task)
                    await self._progress(f" {reason}")
                    self.memory.add_action("finish", {"reason": reason})
                    await self.on_finish(reason)
                    break

                # 5. OBSERVE (после навигационных действий ждём стабильности страницы)
                navigational = action.get("tool") in ("navigate", "click", "click_at", "hover", "drag", "type", "fill_form", "key", "select")
                state_after = await self._observe(wait_stable=navigational)

                # 5.5 STATE DIFF: изменилось ли состояние реально
                diff = diff_states(state, state_after)

                # Повторный diff с задержкой: UI-анимации (модалки) появляются не мгновенно
                if navigational and not diff.get("changed") and state_after is not None:
                    await asyncio.sleep(1.5)
                    state_after2 = await self._observe()
                    if state_after2 is not None:
                        diff2 = diff_states(state, state_after2)
                        if diff2.get("changed"):
                            diff = diff2
                            state_after = state_after2
                            logger.info("State diff: changes detected on delayed re-observe")

                verdict = progress_verdict(diff, action)

                # Ввод текста: прогресс = значение поля установилось (а не diff страницы).
                # Для clear=true проверяем полное совпадение, чтобы не принять дописывание
                # к исходному примеру за успешный ввод.
                if action.get("tool") == "type" and result.get("ok"):
                    # Controller проверяет значение на том же locator до React-
                    # перерисовки. Повторный поиск по старому eid здесь может
                    # ложно провалиться, если редактор выдал новый data-eid.
                    if result.get("verified_on_same_locator"):
                        typed = {"ok": True, "message": "значение установлено и проверено в редакторе"}
                    else:
                        typed = await verify_typed_value(
                            self.controller,
                            action.get("element_id", ""),
                            action.get("text", ""),
                        )
                    if typed["ok"]:
                        self._typed_editors[action.get("element_id", "")] = action.get("text", "")
                        verdict = {"progress": True, "message": typed["message"]}
                    else:
                        result["ok"] = False
                        result["error"] = typed["message"]
                        verdict = {"progress": False, "message": typed["message"]}

                # 6. VERIFY
                verified = await self._verify(action, result, state, state_after)
                result_text = self._format_result(tool, result, verified, verdict, diff)
                await self._progress(f"   {result_text}")

                # 7. GOAL CHECK: runtime определяет успех объективно
                goal_reason = self.goal_detector.check(
                    task, action, result, state, state_after
                )
                if goal_reason:
                    self.state = AgentState.COMPLETED
                    await self._progress(f"[GOAL] Цель достигнута (runtime): {goal_reason}")
                    self.memory.complete_goal(task)
                    self.memory.add_action(tool, {"result": result, "goal_completed": True})
                    await self.on_finish(goal_reason)
                    break

                last_result = result_text

                # Данные от пользователя (код из SMS) → явно в результат,
                # чтобы LM на следующем шаге ввела их в поле
                if result.get("user_input"):
                    last_result = (
                        f" Пользователь передал данные: «{result['user_input']}». "
                        f"Введи их в поле ввода кода (type) и подтверди."
                    )

                # 7.5 Выделенные сообщения страницы → в результат действия.
                # Семантику (ошибка/успех/инфо) оценивает LLM сама по тексту и цвету.
                new_notices = (diff.get("notices_appeared") or []) if diff else []
                if new_notices:
                    last_result = result_text + (
                        " | [NOTICE] На странице появились выделенные сообщения: "
                        + " | ".join(new_notices)
                        + " — прочитай и учти в следующем действии."
                    )
                    await self._progress(f"   [NOTICE] Сообщения: {new_notices[0][:100]}")

                elif tool == "wait_user":
                    self.state = AgentState.RUNNING  # вышли из ожидания

                # 8. Учёт для анти-лупа
                self._record(state, action, state_after)

                self.memory.add_action(tool, {
                    "thought": thought,
                    "params": {k: v for k, v in action.items() if k not in ("tool", "thought", "plan")},
                    "result": result,
                    "verified": verified,
                    "progress": verdict["progress"],
                })

                await asyncio.sleep(0.5)

            else:
                reason = f"Достигнут лимит шагов ({self.max_steps})"
                await self._progress(f" {reason}")
                await self.on_finish(reason)

        except Exception as e:
            logger.error(f"Agent loop error: {e}", exc_info=True)
            self.memory.add_error(str(e))
            await self._progress(f"[ERROR] Ошибка: {e}")
            await self.on_finish(f"Ошибка: {e}")

        finally:
            self.running = False
            self.memory.save()
            self.memory.save_history()

    # ─── Vision: сборка картинки уровня ─────────────────────────────

    async def _capture_vision_image(self) -> tuple:
        """PNG-картинка уровня для vision. Сперва sheet клеток (кропы
        спрайтов + подписи eID); если клеток нет — viewport как PNG.
        Возвращает (b64, order) — order соответствует порядку клеток на sheet
        (строки сверху вниз, слева направо)."""
        try:
            r = await _capture_game_sheet(self.controller, tile=48)
            if r.get("b64") and r.get("order"):
                return r["b64"], r["order"]
            logger.info(f"Sheet unavailable ({r.get('error')}), fallback to viewport PNG")
        except Exception as e:
            logger.warning(f"Sheet build failed: {e} — fallback to viewport PNG")
        try:
            shot = await asyncio.wait_for(
                self.controller.page.screenshot(type="png"),
                timeout=10,
            )
            b64 = base64.b64encode(shot).decode("ascii")
            # Viewport PNG может превышать порог доставки — уменьшать некем,
            # отдаём как есть (лучше шанс, чем ничего).
            return b64, []
        except Exception as e:
            logger.warning(f"Screenshot failed (продолжаю без vision): {e}")
            return "", []

    # ─── Решение с retry ────────────────────────────────────────────

    async def _decide_with_retry(self, task, state, last_result,
                                  rejected_warning: str = "",
                                  user_messages: str = "",
                                  screenshot_b64: str = "",
                                  sheet_order: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Решение LLM с retry при пустом/нечитаемом ответе.
        При vision-запросе retry тоже идёт со скриншотом.
        sheet_order — порядок eID клеток на приложенной картинке."""
        tabs_text = self._format_tabs()
        max_attempts = 3

        for attempt in range(1, max_attempts + 1):
            action = await self.planner.decide(
                task=task,
                browser_state_text=state.to_llm_text(),
                tabs_text=tabs_text,
                last_action_result=last_result,
                recent_history=self.memory.get_recent_actions(5),
                step=self.step_count,
                rejected_warning=rejected_warning,
                user_messages=user_messages,
                screenshot_b64=screenshot_b64,
                sheet_order=sheet_order or [],
            )
            # wait с пометкой "пустой ответ" = LM не сработал. Важно: мысль
            # вида "список элементов пуст" — легитимное решение, не сбой.
            thought = (action.get("thought") or "").lower()
            is_empty_fallback = (
                action.get("tool") == "wait"
                and ("пустой ответ" in thought or "error" in thought
                     or "ошибка llm" in thought or "ошибка lm" in thought
                     or "не удалось распознать" in thought
                     or "не настроен" in thought or "не установлен" in thought)
            )
            if not is_empty_fallback:
                return action

            logger.warning(f"LLM empty/invalid response (attempt {attempt}/{max_attempts})")
            await self._progress(f"    LLM вернул пустой ответ, повтор {attempt}/{max_attempts - 1}")
            await asyncio.sleep(1.5 * attempt)

        return None

    # ─── State-based loop breaker ───────────────────────────────────

    def _state_key(self, state: BrowserState) -> str:
        """Ключ состояния: URL + scroll + первые id элементов"""
        eids = ",".join(el["eid"] for el in state.elements[:15])
        game = state.game or {}
        return f"{state.url}|{state.scroll_y}|{eids}|{game.get('level')}"

    def _action_key(self, action: Dict[str, Any]) -> str:
        key = action.get("tool", "?")
        for param in ("url", "element_id", "key", "tab_id", "direction", "x", "y", "x1", "y1", "x2", "y2"):
            if param in action:
                key += f":{action[param]}"
        return key

    def _record(self, state: BrowserState, action: Dict[str, Any], state_after: Optional[BrowserState]):
        """Записать (состояние, действие) и прогресс"""
        pair = (self._state_key(state), self._action_key(action))
        self._state_action_history.append(pair)
        self._state_action_history = self._state_action_history[-8:]

    def _is_stuck(self, state: BrowserState) -> bool:
        """
        Застряли? Последние 3 записи: то же состояние + то же действие.
        Это значит: действие выполняется, но состояние страницы не меняется.
        """
        if len(self._state_action_history) < 3:
            return False
        last3 = self._state_action_history[-3:]
        first_state, first_action = last3[0]
        return all(s == first_state and a == first_action for s, a in last3)

    async def _apply_recovery(self, state: BrowserState) -> str:
        """
        Recovery-стратегия вместо повторного вопроса к LLM.
        Возвращает текст-подсказку для LLM на следующем шаге, или "stop".
        """
        self._recovery_used += 1
        if self._recovery_used > 3:
            reason = "Агент застрял: 3 recovery-попытки не дали прогресса"
            await self._progress(f"🛑 {reason}")
            await self.on_finish(reason)
            return "stop"

        # Определяем какое действие застряло
        stuck_action = self._state_action_history[-1][1].split(":")[0]

        # Приоритет: обязательная модалка — самая частая причина «клика без
        # прогресса» (модель жмёт крестик, runtime его блокирует).
        if self._dialog_sig(state):
            hint = self._track_dialog(state) or (
                "RECOVERY: открыто обязательное модальное окно. Не отменяй его — "
                "выполни требование окна (заполни поле и нажми «Сохранить и продолжить»)."
            )
            await self._progress("🪟 Требуется действие в модальном окне")
            self._state_action_history = []
            return hint

        await self._progress(f" Нет прогресса после 3× {stuck_action} — recovery #{self._recovery_used}")

        # Сбрасываем историю, чтобы дать LLM шанс с новой подсказкой
        self._state_action_history = []

        if stuck_action == "scroll":
            # Скролл не помогает → подсказываем кликнуть по якорной ссылке
            anchor = self._find_anchor_link(state)
            if anchor:
                hint = (f"RECOVERY: скролл не даёт прогресса. На странице есть ссылка "
                        f"[{anchor['eid']}] «{anchor.get('text', '')}» → {anchor.get('href', '')}. "
                        f"Кликни по ней вместо скролла.")
            else:
                hint = ("RECOVERY: скролл не даёт прогресса. Проанализируй элементы заново "
                        "и выбери конкретное действие (click/type), а не scroll.")
        elif stuck_action in ("type", "fill_form"):
            hint = ("RECOVERY: ввод не подтверждён. НЕ повторяй type в тот же element_id "
                    "и НЕ дописывай код поверх текущего содержимого. Сначала заново "
                    "проверь актуальный element_id и текущее значение поля; для редактора "
                    "кода используй полную замену clear=true. Если значение уже совпадает, "
                    "переходи к кнопке отправки.")
        elif stuck_action in ("click", "click_at", "hover", "drag"):
            hint = ("RECOVERY: игровое действие не дало прогресса. Не повторяй его вслепую: "
                    "сделай новый screenshot/observe, проверь текущий номер уровня и текст задания, "
                    "затем выбери другой элемент или координаты. Для canvas используй click_at/drag, "
                    "для смены состояния — реальные key действия.")
        else:
            hint = f"RECOVERY: действие {stuck_action} повторяется без результата. Смени подход."

        return hint

    def _find_anchor_link(self, state: BrowserState) -> Optional[Dict[str, Any]]:
        """Якорная ссылка (#section) или ссылка с текстом — для recovery после скроллов"""
        for el in state.elements:
            if el.get("tag") == "a" and el.get("href"):
                href = el["href"]
                if "#" in href and not href.endswith("#"):
                    return el
        return None

    # ─── Блокирующие модальные окна (анти-пинг-понг) ────────────────
    # Типичный сбой: обязательная модалка («Уточните данные»), которую агент
    # закрывает крестиком/«Сбросить». Окно тут же открывается снова → шаги
    # уходят в пустую. Рантайм сам распознаёт окно и не даёт его отменять.

    _PROCEED_RE = re.compile(
        r"сохран|продолж|подтвер|примен|отправ|далее|дальше|готово|"
        r"заверш|выбрать|принять|agree|accept|continue|save|confirm|submit|next|apply|done|select|"
        r"(?:^|\s)ок(?:\s|$)|(?:^|\s)okay(?:\s|$)",
        re.I,
    )
    _CANCEL_RE = re.compile(
        r"закрыть|отмен|сброс|не сейчас|позже|close|cancel|dismiss|skip|later|×|✕|✖",
        re.I,
    )

    def _dialog_sig(self, state: BrowserState) -> Optional[str]:
        """Сигнатура открытого блокирующего окна (title + начало текста)."""
        d = state.dialog or {}
        if not d.get("open") or not d.get("blocking"):
            return None
        title = (d.get("title") or "").strip()
        text = re.sub(r"\s+", " ", (d.get("text") or "").strip())[:120]
        return f"{title}|{text}"

    def _track_dialog(self, state: BrowserState) -> Optional[str]:
        """
        Отследить открытия блокирующего окна. Возвращает подсказку, если то же
        окно открылось повторно (агент его уже закрывал, не выполнив требование).
        """
        sig = self._dialog_sig(state)
        prev = self._dialog_prev_sig
        self._dialog_prev_sig = sig
        if sig is None:
            return None
        if sig == prev:
            return None  # окно открыто непрерывно — это то же наблюдение
        # Окно появилось заново (или появилось впервые)
        self._dialog_open_counts[sig] = self._dialog_open_counts.get(sig, 0) + 1
        opens = self._dialog_open_counts[sig]
        if opens < 2:
            return None
        d = state.dialog or {}
        btns = d.get("buttons") or []
        proceed = [b for b in btns if self._PROCEED_RE.search(b.get("text") or "")]
        target = (f"Нажми [{proceed[0]['eid']}] «{proceed[0]['text']}»."
                  if proceed else
                  "Заполни обязательное поле окна и нажми его кнопку подтверждения.")
        hint = (
            f"⛔ ДИАЛОГ-ПИНГ-ПОНГ: окно «{(d.get('title') or d.get('text', ''))[:80]}» "
            f"открылось уже {opens}-й раз. Его НЕЛЬЗЯ закрыть крестиком, «Сбросить», "
            f"Esc или «Не сейчас» — оно открывается снова, и задача не двигается. "
            f"{target} Если для подтверждения нужно значение, которого у тебя нет — "
            f"используй wait_user, а не закрытие окна."
        )
        if sig not in self._dialog_hint_given:
            self._dialog_hint_given.add(sig)
            logger.info(hint[:300])
        return hint

    def _cancel_buttons_in_dialog(self, state: BrowserState) -> List[Dict[str, Any]]:
        """Кнопки отмены внутри открытого блокирующего окна (их клик запрещён)."""
        d = state.dialog or {}
        if not d.get("open") or not d.get("blocking"):
            return []
        out = []
        for el in state.elements:
            # Берём всё, что помечено как элемент диалога: dialog.buttons может
            # не содержать крестик без подписи или кнопки за лимитом среза.
            if not el.get("in_dialog"):
                continue
            tag = el.get("tag")
            is_btn = (tag == "button" or el.get("role") == "button"
                      or (tag == "input" and el.get("type") in ("submit", "button")))
            if not is_btn:
                continue
            label = f"{el.get('text') or ''} {el.get('aria') or ''}"
            if self._CANCEL_RE.search(label):
                out.append(el)
        return out

    def _dialog_guard(self, action: Dict[str, Any], state: BrowserState) -> Optional[Dict[str, Any]]:
        """
        Блокировка клика по «отмене» в обязательной модалке.
        Возвращает результат-ошибку с подсказкой либо None (действие разрешено).
        """
        tool = action.get("tool")
        if tool not in ("click", "click_at"):
            return None
        cancels = self._cancel_buttons_in_dialog(state)
        if not cancels:
            return None

        hit = None
        if tool == "click":
            eid = str(action.get("element_id", ""))
            hit = next((c for c in cancels if c.get("eid") == eid), None)
        else:
            x, y = action.get("x"), action.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                for c in cancels:
                    cx, cy = c.get("x"), c.get("y")
                    w = max(c.get("width") or 0, 44)
                    h = max(c.get("height") or 0, 44)
                    if cx is not None and cy is not None and abs(x - cx) <= w and abs(y - cy) <= h:
                        hit = c
                        break
        if hit is None:
            return None

        d = state.dialog or {}
        proceed = [b for b in (d.get("buttons") or [])
                   if self._PROCEED_RE.search(b.get("text") or "")]
        target = (f"Вместо этого нажми [{proceed[0]['eid']}] «{proceed[0]['text']}»."
                  if proceed else
                  "Вместо этого заполни обязательное поле окна и нажми его кнопку подтверждения.")
        label = (hit.get("text") or hit.get("aria") or hit["eid"]).strip()[:40]
        return {
            "ok": False,
            "error": (
                f"Клик по «{label}» запрещён: это отмена обязательного модального окна — "
                f"оно закроется и откроется снова, задача не выполнится. {target} "
                f"Если нужных данных нет — wait_user."
            ),
        }

    # ─── Наблюдение / выполнение / верификация ──────────────────────

    # ─── Ожидание данных от пользователя ────────────────────────────

    async def _wait_for_user_input(self, timeout: float = 300,
                                    state_before: Optional[BrowserState] = None) -> Optional[str]:
        """
        Пауза цикла до передачи данных пользователем (web_backend →
        provide_user_input) или таймаут.
        Во время паузы каждые 2с проверяем состояние страницы: если пользователь
        сам ввёл код в браузере (состояние изменилось) — выходим сразу.
        Возвращает введённое значение или None.
        """
        self._user_input_event = asyncio.Event()
        self._user_input_value = None
        try:
            wait_task = asyncio.create_task(self._user_input_event.wait())
            poll_task = asyncio.create_task(self._poll_state_during_wait(state_before))
            done, pending = await asyncio.wait(
                {wait_task, poll_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            if wait_task in done:
                return self._user_input_value
            if poll_task in done and poll_task.result():
                # Пользователь вмешался в браузер — выходим без данных
                return None
            return None  # таймаут
        finally:
            self._user_input_event = None

    async def _poll_state_during_wait(self, state_before: Optional[BrowserState],
                                       interval: float = 2.0) -> bool:
        """
        Поллинг состояния страницы во время wait_user.
        Возвращает True, если состояние изменилось (пользователь вмешался).
        """
        if state_before is None:
            # Без точки сравнения просто ждём вечно (прервётся по timeout)
            await asyncio.Event().wait()
            return False
        key_before = self._state_key(state_before)
        while True:
            await asyncio.sleep(interval)
            try:
                state_now = await self._observe()
                if state_now is not None and self._state_key(state_now) != key_before:
                    await self._progress(
                        "[INFO] Обнаружено: пользователь сам изменил страницу во время ожидания "
                        "(ввёл код / прошёл авторизацию). Продолжаю с новым состоянием."
                    )
                    return True
            except Exception:
                pass  # страница перезагружается — продолжаем поллинг

    async def provide_user_input(self, value: str):
        """
        Передача данных от пользователя (из web_backend).
        Если агент ждёт (wait_user) — будим его. Если работает — данные
        попадут в контекст модели на следующем шаге (user_messages).
        """
        self._user_input_value = value
        self._user_messages.append(value)
        if self._user_input_event is not None:
            self._user_input_event.set()
            await self._progress(f"[INFO] Получены данные от пользователя")
        else:
            await self._progress(f"[INFO] Данные от пользователя приняты — агент учтёт на следующем шаге")

    def _drain_user_messages(self) -> str:
        """Забрать накопленные сообщения пользователя для промпта"""
        if not self._user_messages:
            return ""
        msgs = list(self._user_messages)
        self._user_messages.clear()
        return (
            "=== СООБЩЕНИЯ ОТ ПОЛЬЗОВАТЕЛЯ (учти в решении) ===\n"
            + "\n".join(f"[USER] {m}" for m in msgs)
            + "\n"
        )

    # ─── Память об отвергнутых данных ───────────────────────────────
    # Семантику ("это ошибка, данные не подходят") оценивает LLM — она видит
    # текст сообщения. Рантайм только запоминает её вердикт и напоминает.

    def remember_rejected_value(self, value: str, message: str):
        """LM сообщила, что сайт отверг значение — запомнить для будущих шагов"""
        if value:
            self._rejected_credentials[str(value).strip()] = message

    def _rejected_warning(self) -> str:
        """Предупреждение для промпта: какие данные уже отвергнуты сайтом"""
        if not self._rejected_credentials:
            return ""
        lines = ["=== ОТВЕРГНУТЫЕ САЙТОМ ДАННЫЕ (НЕ ИСПОЛЬЗОВАТЬ ПОВТОРНО) ==="]
        for value, msg in self._rejected_credentials.items():
            lines.append(f"[REJECTED] {value} — сайт ответил: {msg[:120]}")
        lines.append("Используй альтернативу (email↔телефон) или finish с reason.")
        return "\n".join(lines) + "\n"

    async def _observe(self, wait_stable: bool = False) -> Optional[BrowserState]:
        """
        Захват BrowserState.
        wait_stable=True — сначала дождаться завершения навигации
        (после click/navigate контекст JS уничтожается и пересоздаётся).
        """
        page = self.controller.page
        if not page:
            return None

        if wait_stable:
            # Таймаут ожидания стабилизации — не блокируем цикл навсегда
            # (CDP-канал может зависнуть; наблюдали на neal.fun)
            import asyncio as _aio
            try:
                await _aio.wait_for(
                    self.controller.wait_for_stable_page(timeout=10), timeout=14
                )
            except _aio.TimeoutError:
                logger.warning("wait_for_stable_page общий таймаут — продолжаю без стабильности")

        # Несколько попыток: контекст может пересоздаваться.
        # Каждый capture ограничен по времени — зависший evaluate не убьёт шаг.
        import asyncio as _aio
        for attempt in range(3):
            try:
                return await _aio.wait_for(capture_browser_state(page), timeout=15)
            except _aio.TimeoutError:
                logger.warning(f"Observe attempt {attempt + 1}: capture завис (>15с)")
                await asyncio.sleep(1.0 + attempt)
            except Exception as e:
                logger.warning(f"Observe attempt {attempt + 1} failed: {e}")
                await asyncio.sleep(1.0 + attempt)

        logger.error("Observe failed after 3 attempts")
        return None

    async def _revive_chrome(self) -> Optional[BrowserState]:
        """Chrome умер (TAL/краш/закрыт) — перезапускаем с CDP и переподключаемся.
        Возвращает свежий BrowserState или None, если оживить не удалось.
        Проверяет не только открытый сокет, но и реальный HTTP-ответ CDP:
        за мгновение до смерти порт успевает принять соединение (гонка 00:30)."""
        import socket
        import asyncio as _aio
        import urllib.request

        def _cdp_responsive() -> bool:
            # Сокет-проверка ложно-положительна при смерти Chrome — гоняем HTTP
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.controller.cdp_port}/json/version", timeout=3
                ) as resp:
                    return resp.status == 200
            except Exception:
                return False

        def _chrome_process_alive() -> bool:
            try:
                probe = subprocess.run(
                    ["pgrep", "-x", "Google Chrome"], capture_output=True, timeout=5
                )
                return probe.returncode == 0
            except Exception:
                return False

        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if not Path(chrome_path).exists():
            import shutil
            chrome_path = shutil.which("chrome") or shutil.which("google-chrome") or chrome_path

        profile_dir = Path(__file__).parent.parent / "ai_browser_profile"
        profile_dir.mkdir(exist_ok=True)

        if _cdp_responsive():
            # Порт отвечает HTTP 200 — Chrome жив, revive не нужен
            return None

        if _chrome_process_alive():
            # Классическая ловушка macOS: Chrome жив, но без CDP-флагов
            # (запущен вручную/LaunchServices). Одиночный процесс — второй
            # запуск просто откроет окно, порт не поднимется. Перезапускаем.
            await self._progress("[WARN] Chrome жив, но без CDP — перезапускаю с отладкой")
            logger.warning("Chrome alive without CDP — quitting via osascript")
            try:
                subprocess.run(
                    ["osascript", "-e", 'tell application "Google Chrome" to quit'],
                    capture_output=True, timeout=10,
                )
            except Exception as e:
                logger.warning(f"osascript quit failed: {e}")
            # Ждём фактического завершения процесса
            for _ in range(12):
                if not _chrome_process_alive():
                    break
                await _aio.sleep(1)
            if _chrome_process_alive():
                logger.error("Chrome did not quit in time — cannot relaunch")
                await self._progress("[ERROR] Chrome не завершился — перезапуск невозможен")
                return None

        await self._progress("[WARN] Chrome не отвечает — перезапускаю браузер")
        logger.warning("Chrome CDP port closed — relaunching browser")
        try:
            await self.controller.disconnect()
        except Exception:
            pass

        try:
            subprocess.Popen(
                [chrome_path,
                 f"--user-data-dir={profile_dir}",
                 f"--remote-debugging-port={self.controller.cdp_port}",
                 "--no-first-run",
                 "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error(f"Failed to relaunch Chrome: {e}")
            return None

        # Ждём не просто открытого порта, а работающего CDP (HTTP 200)
        for _ in range(30):
            if _cdp_responsive():
                break
            await _aio.sleep(1)
        else:
            logger.error("Chrome CDP not ready after relaunch")
            return None

        try:
            await self.controller.connect(mode="attach")
            await self.controller.navigate("https://neal.fun/not-a-robot/")
            await _aio.sleep(2)
            return await self._observe()
        except Exception as e:
            logger.error(f"Revive reconnect failed: {e}")
            return None

    def _format_tabs(self) -> str:
        tabs = self.controller._tabs
        if not tabs:
            return "(нет вкладок)"
        return "\n".join(
            f"{'→ ' if t.is_active else '  '}[{t.tab_id}] {t.title[:60]} — {t.url[:80]}"
            for t in tabs[:10]
        )

    async def _execute(self, action: Dict[str, Any], state: BrowserState) -> Dict[str, Any]:
        tool = action.get("tool")
        try:
            if tool == "navigate":
                nav_result = await self.controller.navigate(action["url"])
                if not nav_result.get("ok"):
                    return nav_result  # {"ok": False, "error": "ERR_NAME_NOT_RESOLVED: ..."}
                return {"ok": True, "detail": f"Переход: {action['url']}"}

            elif tool == "click":
                eid, err = self._resolve_eid(
                    state, action.get("element_id", ""),
                    text_fallback=bool(action.get("text_fallback")),
                )
                if err:
                    return {"ok": False, "error": err}
                # Кладём актуальный id обратно: verify_typed_value и память
                # дальше работают с action, а не с локальной переменной.
                action["element_id"] = eid
                result = await self.controller.click_element(eid)
                return result

            elif tool in ("click_at", "hover", "drag"):
                # Мышинные действия с жёстким таймаутом: зависший mouse-вызов
                # навсегда блокирует шаг (наблюдали на игровых canvas).
                import asyncio as _aio

                async def _mouse():
                    if tool == "click_at":
                        return await self.controller.click_at(action.get("x", 0), action.get("y", 0))
                    if tool == "hover":
                        return await self.controller.hover_at(action.get("x", 0), action.get("y", 0))
                    return await self.controller.drag_at(
                        action.get("x1", 0), action.get("y1", 0),
                        action.get("x2", 0), action.get("y2", 0), action.get("steps", 12)
                    )

                try:
                    return await _aio.wait_for(_mouse(), timeout=20)
                except _aio.TimeoutError:
                    return {"ok": False, "error": f"{tool} превысил 20с — страница не отвечает на мышь"}

            elif tool == "type":
                eid, err = self._resolve_eid(
                    state, action.get("element_id", ""),
                    text_fallback=bool(action.get("text_fallback")),
                )
                if err:
                    return {"ok": False, "error": err}
                action["element_id"] = eid  # актуальный id для verify/памяти
                result = await self.controller.type_into_element(
                    eid, action.get("text", ""), clear=action.get("clear", True)
                )
                if result.get("ok") and action.get("submit"):
                    await self.controller.press_key("Enter")
                    result["submitted"] = True
                return result

            elif tool == "fill_form":
                return await self._execute_fill_form(action, state)

            elif tool == "key":
                await self.controller.press_key(action.get("key", "Enter"))
                return {"ok": True, "detail": f"Клавиша: {action.get('key')}"}

            elif tool == "scroll":
                await self.controller.scroll(
                    action.get("direction", "down"), action.get("amount", 600)
                )
                return {"ok": True, "detail": f"Прокрутка {action.get('direction')}"}

            elif tool == "select":
                return await self.controller.select_option(
                    action.get("element_id", ""), action.get("value", "")
                )

            elif tool == "new_tab":
                tab = await self.controller.new_tab(action.get("url") or None)
                return {"ok": True, "detail": f"Новая вкладка [{tab.tab_id}]: {tab.url}"}

            elif tool == "switch_tab":
                await self.controller.switch_tab(action.get("tab_id", ""))
                return {"ok": True, "detail": f"Вкладка: {action.get('tab_id')}"}

            elif tool == "close_tab":
                await self.controller.close_tab(action.get("tab_id", ""))
                return {"ok": True, "detail": f"Закрыта: {action.get('tab_id')}"}

            elif tool == "extract":
                text = await self.controller.extract_text(action.get("selector", "body"))
                return {"ok": True, "detail": f"Извлечено {len(text)} символов", "text": text[:1000]}

            elif tool == "wait":
                await asyncio.sleep(action.get("seconds", 2))
                return {"ok": True, "detail": f"Ожидание {action.get('seconds')}с"}

            elif tool == "wait_user":
                # Ожидание данных от пользователя (код из SMS, push, пароль).
                # Агент не может их получить сам — пауза до передачи через UI.
                # Во время паузы поллим состояние страницы: если пользователь
                # сам ввёл код в браузере — выходим из ожидания сразу.
                self.state = AgentState.WAITING_USER
                reason = action.get("reason") or "нужны данные от пользователя"
                await self._progress(
                    f"[WAIT] ОЖИДАНИЕ ДАННЫХ ОТ ПОЛЬЗОВАТЕЛЯ: {reason}\n"
                    f"   Передай данные через поле ввода (кнопка «Передать агенту»), "
                    f"или введи их в браузер сам — агент продолжит после этого."
                )
                got = await self._wait_for_user_input(
                    timeout=action.get("timeout", 300),
                    state_before=state,
                )
                if got:
                    return {"ok": True, "detail": f"Получены данные от пользователя ({len(got)} симв.)",
                            "user_input": got}
                # Данных не передано. Таймаут НЕ ошибка: пользователь мог сам
                # завершить вход. Goal check по новому состоянию решает —
                # цель достигнута (finish) или продолжаем цикл.
                state_now = await self._observe()
                if state_now is not None:
                    goal_reason = self.goal_detector.check_state(task, state_now)
                    if goal_reason:
                        return {"ok": True, "detail": f"Цель достигнута во время ожидания: {goal_reason}",
                                "goal_completed": True, "goal_reason": goal_reason}
                    if self._state_key(state_now) != self._state_key(state):
                        return {"ok": True, "detail": "Пользователь сам изменил страницу во время ожидания"}
                return {"ok": False, "error": "Пользователь не передал данные за отведённое время"}

            elif tool == "evaluate":
                result = await self.controller.evaluate_js(action.get("expression", ""))
                return {"ok": True, "detail": "JS выполнен", "result": str(result)[:500]}

            else:
                return {"ok": False, "error": f"Неизвестный tool: {tool}"}

        except Exception as e:
            logger.error(f"Execute {tool} error: {e}")
            return {"ok": False, "error": str(e)}

    async def _execute_fill_form(self, action: Dict[str, Any], state: BrowserState) -> Dict[str, Any]:
        """Композитное действие: заполнить поля формы и отправить"""
        fields: Dict[str, str] = action.get("fields", {})
        form_id = action.get("form_id", "")
        filled, missed = [], []

        for field_name, value in fields.items():
            eid = self._find_field_element(state, field_name, form_id)
            if eid:
                r = await self.controller.type_into_element(eid, str(value), clear=True)
                if r.get("ok"):
                    filled.append(field_name)
                else:
                    missed.append(field_name)
            else:
                missed.append(field_name)

        if missed:
            return {"ok": len(filled) > 0, "detail": f"Заполнено: {filled}, не найдено: {missed}"}

        if action.get("submit", True):
            submit_eid = self._find_submit_button(state, form_id)
            if submit_eid:
                await asyncio.sleep(0.3)
                r = await self.controller.click_element(submit_eid)
                if r.get("ok"):
                    return {"ok": True, "detail": f"Форма заполнена ({', '.join(filled)}) и отправлена", "submitted": True}
            if filled:
                await self.controller.press_key("Enter")
                return {"ok": True, "detail": f"Форма заполнена ({', '.join(filled)}), отправлена через Enter", "submitted": True}

        return {"ok": True, "detail": f"Заполнено: {', '.join(filled)} (без отправки)"}

    def _find_field_element(self, state: BrowserState, field_name: str, form_id: str = "") -> Optional[str]:
        name_lower = field_name.lower()
        candidates = []
        for el in state.elements:
            if el.get("tag") not in ("input", "textarea", "select"):
                continue
            if form_id and el.get("form") and el["form"] != form_id:
                continue
            score = 0
            if (el.get("name") or "").lower() == name_lower:
                score = 4
            elif (el.get("id_attr") or "").lower() == name_lower:
                score = 3
            elif name_lower in (el.get("name") or "").lower():
                score = 2
            elif name_lower in (el.get("placeholder") or "").lower():
                score = 1
            elif name_lower in (el.get("aria") or "").lower():
                score = 1
            if score:
                candidates.append((score, el["eid"]))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        return None

    def _find_submit_button(self, state: BrowserState, form_id: str = "") -> Optional[str]:
        submit_keywords = ("submit", "send", "отправить", "отправка", "sign in", "log in",
                           "войти", "register", "next", "далее", "ok", "поиск")
        best = None
        for el in state.elements:
            if el.get("tag") not in ("button", "input"):
                continue
            if el.get("type") == "submit":
                if not form_id or not el.get("form") or el["form"] == form_id:
                    return el["eid"]
            text = (el.get("text") or "").lower()
            if any(kw in text for kw in submit_keywords):
                if not form_id or not el.get("form") or el["form"] == form_id:
                    best = best or el["eid"]
        return best

    def _find_element_by_text(self, state: BrowserState, text: str) -> Optional[Dict[str, Any]]:
        text_lower = text.lower().strip()
        for el in state.elements:
            el_text = (el.get("text") or "").lower()
            el_aria = (el.get("aria") or "").lower()
            if text_lower and (text_lower in el_text or text_lower in el_aria):
                return el
        return None

    _EID_RE = re.compile(r"^e\d+$", re.I)
    # Заглушка вместо id: 'e?', 'e', 'e__', 'e*' — модель не подобрала номер.
    _EID_PLACEHOLDER_RE = re.compile(r"^e[\W_]*$", re.I)

    def _resolve_eid(self, state: BrowserState, raw: Any,
                     text_fallback: bool = False) -> tuple:
        """
        Элемент из ответа LLM → реальный eid. Возвращает (eid, error).
        Модель иногда шлёт плейсхолдер ('e?') или подпись кнопки вместо id.
        Различаем: похож на id → его нет в снимке; иначе ищем по тексту.
        """
        raw = str(raw or "").strip()
        if not raw:
            return None, "Не указан element_id — возьми id из списка элементов состояния."
        if state.find_element(raw) is not None:
            return raw, None
        if self._EID_PLACEHOLDER_RE.match(raw):
            return None, (f"element_id '{raw}' — незаполненная заглушка. "
                          f"Возьми точный id (e1, e2, ...) из списка элементов состояния.")
        cleaned = raw.strip("?*# ").strip()
        if self._EID_RE.match(cleaned) and not text_fallback:
            return None, (f"Элемента с id '{raw}' нет в текущем состоянии. "
                          f"Id пересоздаются после каждого действия — возьми актуальный "
                          f"из списка элементов.")
        found = self._find_element_by_text(state, cleaned)
        if found:
            return found["eid"], None
        return None, (f"Элемент '{raw}' не найден (ни по id, ни по тексту). "
                      f"Используй точный id из списка элементов.")

    async def _verify(self, action, result, state_before, state_after) -> str:
        tool = action.get("tool")
        if not result.get("ok"):
            return f"[ERROR] {result.get('error', 'действие не выполнено')}"
        if tool == "navigate":
            if state_after and state_after.url and not state_after.url.startswith("about:"):
                return f" Страница загружена: {state_after.title[:60]}"
            return " Навигация выполнена, страница пустая"
        if tool == "click":
            if state_after is None:
                return " Клик выполнен (страница в навигации)"
            if state_after.url != state_before.url:
                return f" Клик привёл к переходу: {state_after.url[:80]}"
            return " Клик выполнен"
        if tool == "type":
            return " Текст введён" + (" + Enter" if result.get("submitted") else "")
        return " Выполнено"

    def _format_result(self, tool: str, result: Dict[str, Any], verified: str,
                       verdict: Optional[Dict[str, Any]] = None,
                       diff: Optional[Dict[str, Any]] = None) -> str:
        # Ошибка действия (DNS, элемент не найден) — важнее всего
        if not result.get("ok"):
            return f"[ERROR] {result.get('error', 'действие не выполнено')}"
        text = verified
        detail = result.get("detail") or ""
        if detail and detail not in verified:
            text += f" ({detail[:100]})"
        # Вердикт о прогрессе: LLM должен знать, сработало ли действие реально
        if verdict and not verdict["progress"]:
            text += f" |  NO_PROGRESS: {verdict['message']}"
        elif verdict and diff and diff.get("changed"):
            text += f" |  {diff.get('summary', '')[:150]}"
        return text

    async def _progress(self, text: str):
        logger.info(text)
        try:
            await self.on_progress(text)
        except Exception as e:
            logger.warning(f"Progress callback error: {e}")

    def stop(self):
        self.running = False
        logger.info("Agent loop stopped")